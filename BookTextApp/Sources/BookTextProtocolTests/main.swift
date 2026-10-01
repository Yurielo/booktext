import Foundation
import BookTextCore

assert(AutomaticProcessingOptions.arguments(includeSearchablePDF: false) == ["--ocr", "auto", "--no-descriptions", "--no-pdf"])
assert(!AutomaticProcessingOptions.arguments(includeSearchablePDF: true).contains("--no-qa"))
assert(!AutomaticProcessingOptions.arguments(includeSearchablePDF: true).contains("--raw-ocr"))

private final class EventCollector: @unchecked Sendable {
    private let lock = NSLock()
    private var values: [MachineEvent] = []
    func append(_ value: MachineEvent) { lock.lock(); values.append(value); lock.unlock() }
    func snapshot() -> [MachineEvent] { lock.lock(); defer { lock.unlock() }; return values }
}

private func event(_ type: String, code: String = "ok", phase: String? = "ocr") throws -> MachineEvent {
        try JSONDecoder().decode(MachineEvent.self, from: Data("{\"protocol_version\":\"1.0\",\"job_id\":\"job-1\",\"event_type\":\"\(type)\",\"timestamp\":\"2026-01-01T00:00:00Z\",\"phase\":\"\(phase ?? "")\",\"current\":1,\"total\":2,\"page\":1,\"code\":\"\(code)\",\"message\":\"message\",\"details\":{}}".utf8))
}

func check(_ condition: @autoclosure () -> Bool, _ message: String) throws {
    guard condition() else { throw NSError(domain: "BookTextProtocolTests", code: 1, userInfo: [NSLocalizedDescriptionKey: message]) }
}

func protocolFixtureAndUnknownEvent() throws {
        let path = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().appendingPathComponent("tests/fixtures/machine_protocol/small-success.jsonl")
        let events = try String(contentsOf: path).split(separator: "\n").map { try JSONDecoder().decode(MachineEvent.self, from: Data($0.utf8)) }
        try check(events.first?.eventType == .jobStarted, "fixture must start a job")
        try check(events.allSatisfy(\.supported), "fixture must use protocol 1.x")
        let unknown = try event("future_event")
        try check(unknown.eventType == .unknown, "unknown event must not crash")
}

func progressCompletionFailureAndCancellationTransitions() throws {
        var state = JobPresentation(); state.consume(try event("job_started")); try check(state.state == .running, "start transition")
        state.consume(try event("progress")); try check(state.current == 1 && state.total == 2, "progress values")
        state.consume(try event("warning")); try check(state.warnings == ["message"], "warning transition")
        state.consume(try event("job_completed")); try check(state.state == .completed, "completion transition")
        state.consume(try event("job_failed", code: "processing_failure")); try check(state.state == .failed && state.errorCode == "processing_failure", "failure transition")
        state.consume(try event("job_cancelled", code: "cancelled")); try check(state.state == .cancelled, "cancellation transition")
}

func safeArgumentArrayAndArtifactMapping() throws {
        let config = CoreConfiguration(scriptPath: "/tmp/a path/booktext.py"); let command = config.command(["inspect", "/tmp/out;not-shell"])
        try check(command.0.path == "/usr/bin/env", "env executable")
        try check(command.1 == ["python3", "/tmp/a path/booktext.py", "machine", "inspect", "/tmp/out;not-shell"], "arguments must remain separate argv values")
}

func reviewPayloadFixtureDecodesWithoutTerminalParsing() throws {
    let payload = try JSONDecoder().decode(JSONValue.self, from: Data("{\"item_id\":\"p319-fallback\",\"source_asset\":{\"page_image\":\"pages/page-319.png\",\"crop\":null},\"reasons\":[\"not_verified_structured_data\"],\"status\":{\"status\":\"unresolved\"}}".utf8))
    let object = try unwrap(payload.object, "review object")
    try check(object["item_id"]?.string == "p319-fallback", "stable review item ID")
    try check(object["source_asset"]?.object?["crop"] == .null, "fallback uses no fabricated crop")
    try check(object["reasons"]?.values?.first?.string == "not_verified_structured_data", "review reasons are structured")
}

func shortLivedCoreDeliversItsFinalJSONLEvent() throws {
    let folder = URL(fileURLWithPath: NSTemporaryDirectory()).appendingPathComponent("booktext-bridge-\(UUID().uuidString)")
    try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: folder) }
    let script = folder.appendingPathComponent("core.py")
    let line = "{\\\"protocol_version\\\":\\\"1.0\\\",\\\"job_id\\\":\\\"fast-job\\\",\\\"event_type\\\":\\\"job_completed\\\",\\\"timestamp\\\":\\\"2026-01-01T00:00:00Z\\\",\\\"phase\\\":\\\"review\\\",\\\"current\\\":null,\\\"total\\\":null,\\\"page\\\":null,\\\"code\\\":\\\"ok\\\",\\\"message\\\":\\\"done\\\",\\\"details\\\":{}}"
    try "import os\nos.write(1, b'\(line)\\n')\n".write(to: script, atomically: true, encoding: .utf8)
    let finished = DispatchSemaphore(value: 0)
    let events = EventCollector()
    let bridge = CoreProcessBridge(configuration: CoreConfiguration(scriptPath: script.path))
    try bridge.start(arguments: ["inspect", folder.path], event: { value in events.append(value) }, stderr: { _ in }, finished: { _ in finished.signal() })
    try check(finished.wait(timeout: .now() + 5) == .success, "short-lived Core must finish")
    try check(events.snapshot().contains(where: { $0.eventType == .jobCompleted && $0.jobID == "fast-job" }), "final JSONL event must reach the UI")
}

func documentCentricStateMapping() throws {
    func inspection(high: Int = 0, medium: Int = 0, final: Bool = false, cdm: Bool = false, translation: Bool = false) -> [String: JSONValue] {
        [
            "run_summary": .object(["processed": .number(7)]),
            "review": .object(["unresolved_priority_counts": .object(["HIGH": .number(Double(high)), "MEDIUM": .number(Double(medium))])]),
            "cdm": .object(["present": .bool(cdm)]),
            "artifacts": .object([
                "final_build": .object(["exists": .bool(final)]),
                "cdm": .object(["exists": .bool(cdm)]),
                "translations": .object(["exists": .bool(translation)])
            ])
        ]
    }
    func state(source: Bool = false, output: Bool = false, job: JobPresentationState = .idle, data: [String: JSONValue] = [:], translating: Bool = false, resumable: Bool = false) -> DocumentPresentationSnapshot {
        DocumentPresentationMapper.snapshot(sourceSelected: source, outputSelected: output, jobState: job, inspection: data, isTranslating: translating, translationResumable: resumable)
    }

    try check(state().state == .noDocument, "no-document state")
    try check(state(source: true).state == .readyToProcess, "selected-source state")
    try check(state(source: true, job: .running).state == .processing, "processing state")
    try check(state(source: true, job: .cancelled).state == .interruptedResumable, "resume state")
    try check(state(output: true, job: .completed, data: inspection(high: 2, medium: 3)).state == .reviewRequired, "review-required state")
    try check(state(output: true, job: .completed, data: inspection()).state == .readyForFinalBuild, "ready-for-final-build state")
    try check(state(output: true, job: .completed, data: inspection(final: true, cdm: true)).state == .bookReady, "book-ready state")
    try check(state(output: true, job: .completed, data: inspection(final: true, cdm: true), translating: true).state == .translating, "translating state")
    try check(state(output: true, job: .completed, data: inspection(final: true, cdm: true, translation: true)).state == .translationReady, "translation-ready state")
    try check(state(output: true, job: .failed, data: inspection()).state == .failed, "error state")
    try check(state(output: true, job: .completed, data: inspection(high: 1)).primaryAction == .reviewItems, "review primary action")
    try check(state(output: true, job: .completed, data: inspection(final: true, cdm: true)).primaryAction == .openBook, "open-book primary action")

    let beforeReview = state(output: true, job: .completed, data: inspection(high: 1))
    let afterReview = state(output: true, job: .completed, data: inspection())
    try check(beforeReview.state == .reviewRequired && afterReview.state == .readyForFinalBuild, "Core refresh after Review must change presentation without Swift decision state")
    try check(DocumentPresentationMapper.phaseLabel("ocr") == "Recognizing text", "human-readable OCR phase")
    try check(DocumentPresentationMapper.phaseLabel("translation") == "Translating book", "human-readable translation phase")
}

func recentsAndExportSelectionArePresentationOnly() throws {
    let recents = RecentOutputList.normalized(["/tmp/one", "/tmp/two", "/tmp/one"], adding: "/tmp/three", limit: 3)
    try check(recents == ["/tmp/three", "/tmp/one", "/tmp/two"], "recents must be bounded and deduplicated paths only")
    let formats = ExportFormatSelection.ordered(Set(["pdf", "docx", "invalid"]))
    try check(formats == ["docx", "pdf"], "export sheet must pass only supported selected formats in stable order")
}

func unwrap<T>(_ value: T?, _ message: String) throws -> T {
    guard let value else { throw NSError(domain: "BookTextProtocolTests", code: 1, userInfo: [NSLocalizedDescriptionKey: message]) }
    return value
}

do {
    try protocolFixtureAndUnknownEvent(); try progressCompletionFailureAndCancellationTransitions(); try safeArgumentArrayAndArtifactMapping(); try reviewPayloadFixtureDecodesWithoutTerminalParsing(); try shortLivedCoreDeliversItsFinalJSONLEvent(); try documentCentricStateMapping(); try recentsAndExportSelectionArePresentationOnly()
    print("BookTextProtocolTests: PASS")
} catch { fputs("BookTextProtocolTests: FAIL \(error.localizedDescription)\n", stderr); exit(1) }
