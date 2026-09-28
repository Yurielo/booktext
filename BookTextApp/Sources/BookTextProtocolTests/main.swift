import Foundation
import BookTextCore

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

do {
    try protocolFixtureAndUnknownEvent(); try progressCompletionFailureAndCancellationTransitions(); try safeArgumentArrayAndArtifactMapping()
    print("BookTextProtocolTests: PASS")
} catch { fputs("BookTextProtocolTests: FAIL \(error.localizedDescription)\n", stderr); exit(1) }
