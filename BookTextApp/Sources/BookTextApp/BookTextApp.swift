import SwiftUI
import AppKit
import UniformTypeIdentifiers
import BookTextCore

@main struct BookTextApp: App {
    @StateObject private var model = BookTextModel()
    var body: some Scene { WindowGroup { ContentView().environmentObject(model).frame(minWidth: 720, minHeight: 520) } }
}

@MainActor final class BookTextModel: ObservableObject {
    @Published var corePath = UserDefaults.standard.string(forKey: "booktext.core.path") ?? CoreConfiguration.defaultValue.scriptPath { didSet { UserDefaults.standard.set(corePath, forKey: "booktext.core.path") } }
    @Published var sourceURLs: [URL] = []
    @Published var presentation = JobPresentation()
    @Published var ready = false
    @Published var errorText = ""
    @Published var warningText = ""
    // Core diagnostics are intentionally kept separate from primary UI. The
    // app consumes state only from JSONL, never from human terminal prose.
    @Published private(set) var diagnostics = ""
    @Published var result: [String: JSONValue] = [:]
    @Published var outputURL: URL?
    @Published var useTesseract = true
    @Published var includePDF = false
    private var bridge: CoreProcessBridge?
    private var startedAt: Date?

    var elapsed: String { guard let startedAt else { return "" }; return "Elapsed \(Int(Date().timeIntervalSince(startedAt)))s" }
    func addFiles(_ urls: [URL]) { sourceURLs = urls; ready = false; result = [:]; errorText = ""; preflight() }
    func preflight() {
        presentation.state = .preflighting; errorText = ""; ready = false
        let client = CoreProcessBridge(configuration: CoreConfiguration(scriptPath: corePath)); bridge = client
        do { try client.start(arguments: ["preflight", "--operation", "run"], event: { [weak self] event in Task { @MainActor in self?.receivePreflight(event) } }, stderr: { [weak self] text in Task { @MainActor in self?.receiveStderr(text) } }, finished: { [weak self] status in Task { @MainActor in if status != 0 && self?.errorText.isEmpty == true { self?.errorText = "Preflight failed (exit \(status))." } } }) }
        catch { errorText = error.localizedDescription; presentation.state = .failed }
    }
    private func receivePreflight(_ event: MachineEvent) { Task { @MainActor in
        presentation.consume(event)
        if event.eventType == .jobCompleted { ready = event.details.object?["ok"]?.bool == true; presentation.state = ready ? .ready : .failed }
        if event.eventType == .jobFailed { errorText = "\(event.code): \(event.message)" }
    } }
    func start() {
        guard !sourceURLs.isEmpty, ready else { return }
        presentation = JobPresentation(state: .running); errorText = ""; warningText = ""; startedAt = Date()
        let options = ["run"] + sourceURLs.map(\.path) + ["--"] + (useTesseract ? ["--ocr", "tesseract"] : []) + ["--no-qa"] + (includePDF ? [] : ["--no-pdf"]) + ["--raw-ocr"]
        let client = CoreProcessBridge(configuration: CoreConfiguration(scriptPath: corePath)); bridge = client
        do { try client.start(arguments: options, event: { [weak self] event in Task { @MainActor in self?.receiveRun(event) } }, stderr: { [weak self] text in Task { @MainActor in self?.receiveStderr(text) } }, finished: { [weak self] status in Task { @MainActor in self?.finishRun(status) } }) }
        catch { errorText = error.localizedDescription; presentation.state = .failed }
    }
    private func receiveRun(_ event: MachineEvent) { Task { @MainActor in
        presentation.consume(event)
        if event.eventType == .warning { warningText += (warningText.isEmpty ? "" : "\n") + event.message }
        if let path = event.details.object?["output_dir"]?.string { outputURL = URL(fileURLWithPath: path); UserDefaults.standard.set(path, forKey: "booktext.last.output") }
        if event.eventType == .jobFailed { errorText = "\(event.code): \(event.message)" }
        if event.eventType == .jobCancelled { presentation.state = .cancelled }
    } }
    private func receiveStderr(_ text: String) { Task { @MainActor in if !text.isEmpty { diagnostics += text } } }
    private func finishRun(_ status: Int32) {
        guard presentation.state != .cancelled else { return }
        if status == 0, let outputURL { inspect(outputURL) } else if presentation.state != .failed { presentation.state = .failed; errorText = "processing_failure: Core exited with \(status)." }
    }
    func cancel() { guard bridge?.isRunning == true else { return }; presentation.state = .cancelling; bridge?.cancel() }
    func inspect(_ url: URL) {
        let client = CoreProcessBridge(configuration: CoreConfiguration(scriptPath: corePath)); bridge = client
        do { try client.start(arguments: ["inspect", url.path], event: { [weak self] event in Task { @MainActor in
            self?.presentation.consume(event); if event.eventType == .jobCompleted { self?.result = event.details.object ?? [:]; self?.presentation.state = .completed }
        } }, stderr: { [weak self] text in Task { @MainActor in self?.receiveStderr(text) } }, finished: { _ in }) } catch { errorText = error.localizedDescription }
    }
    func reveal() { if let outputURL { NSWorkspace.shared.activateFileViewerSelecting([outputURL]) } }
    func openPrimary() { guard let outputURL, let artifacts = result["artifacts"]?.object else { return }; let key = artifacts["clean_text"]?.object?["exists"]?.bool == true ? "clean_text" : "markdown"; if let relative = artifacts[key]?.object?["path"]?.string { NSWorkspace.shared.open(outputURL.appendingPathComponent(relative)) } }
    func openReview() { guard let outputURL else { return }; let config = CoreConfiguration(scriptPath: corePath); let command = config.command(["review", outputURL.path]); let process = Process(); process.executableURL = command.0; process.arguments = ["python3", corePath, "review", outputURL.path]; try? process.run() }
}

struct ContentView: View {
    @EnvironmentObject var model: BookTextModel
    @State private var dragging = false
    var body: some View { VStack(alignment: .leading, spacing: 16) {
        Text("BookText").font(.largeTitle.bold()).accessibilityAddTraits(.isHeader)
        Text("Native macOS shell for the local BookText Core").foregroundStyle(.secondary)
        GroupBox("BookText Core") { TextField("Path to booktext.py", text: $model.corePath).textFieldStyle(.roundedBorder); Button("Check Preflight") { model.preflight() }.disabled(model.presentation.state == .running) }
        GroupBox("Source") { VStack(alignment: .leading) {
            Text(model.sourceURLs.isEmpty ? "Drop a PDF, DjVu, or image here, or choose files." : model.sourceURLs.map(\.lastPathComponent).joined(separator: ", "))
            HStack { Button("Choose File(s)") { chooseFiles() }; Toggle("Use Tesseract (quick local run)", isOn: $model.useTesseract); Toggle("Create searchable PDF", isOn: $model.includePDF) }
        }.padding(10).frame(maxWidth: .infinity).background(dragging ? Color.accentColor.opacity(0.15) : .clear).onDrop(of: [.fileURL], isTargeted: $dragging) { providers in
            guard let provider = providers.first else { return false }; provider.loadItem(forTypeIdentifier: UTType.fileURL.identifier, options: nil) { item, _ in if let data = item as? Data, let url = URL(dataRepresentation: data, relativeTo: nil) { Task { @MainActor in model.addFiles([url]) } } }; return true
        } }
        status
        Spacer()
    }.padding(24) }
    @ViewBuilder var status: some View {
        switch model.presentation.state {
        case .idle: Text("Choose an input to begin.")
        case .preflighting: ProgressView("Checking local Core prerequisites…")
        case .ready: Button("Start BookText") { model.start() }.buttonStyle(.borderedProminent).disabled(model.sourceURLs.isEmpty)
        case .running, .cancelling: VStack(alignment: .leading) { ProgressView(value: Double(model.presentation.current ?? 0), total: Double(max(model.presentation.total ?? 1, 1))); Text("\(model.presentation.phase ?? "working")  page \(model.presentation.page.map(String.init) ?? "—")  \(model.elapsed)"); Button(model.presentation.state == .cancelling ? "Cancelling…" : "Cancel") { model.cancel() }.disabled(model.presentation.state == .cancelling) }
        case .cancelled: Label("Cancelled — Core cleanup completed.", systemImage: "xmark.circle").foregroundStyle(.orange)
        case .completed: VStack(alignment: .leading) { Label("Completed", systemImage: "checkmark.circle.fill").foregroundStyle(.green); if let url = model.outputURL { Text(url.path).textSelection(.enabled) }; resultSummary; HStack { Button("Reveal in Finder") { model.reveal() }; Button("Open Primary Result") { model.openPrimary() }; Button("Open Review") { model.openReview() } } }
        case .failed: VStack(alignment: .leading) { Label("\(model.errorText)", systemImage: "exclamationmark.triangle.fill").foregroundStyle(.red).textSelection(.enabled); Button("Retry Preflight") { model.preflight() } }
        }
        if !model.warningText.isEmpty { Text(model.warningText).font(.caption).foregroundStyle(.orange).lineLimit(4).textSelection(.enabled) }
    }
    @ViewBuilder var resultSummary: some View {
        let pages = model.result["run_summary"]?.object
        let review = model.result["review"]?.object?["unresolved_priority_counts"]?.object
        let cdm = model.result["cdm"]?.object?["present"]?.bool == true
        let exports = model.result["exports"]?.object?["present"]?.bool == true
        Text("Pages processed: \(pages?["processed"]?.integer.map(String.init) ?? "—") · Review HIGH/MEDIUM: \(review?["HIGH"]?.integer.map(String.init) ?? "0") / \(review?["MEDIUM"]?.integer.map(String.init) ?? "0")")
        Text("CDM: \(cdm ? "available" : "not built") · Exports: \(exports ? "available" : "not built")").foregroundStyle(.secondary)
    }
    private func chooseFiles() { let panel = NSOpenPanel(); panel.allowsMultipleSelection = true; panel.canChooseDirectories = false; panel.allowedContentTypes = [.pdf, .image, UTType(filenameExtension: "djvu") ?? .data, UTType(filenameExtension: "djv") ?? .data]; if panel.runModal() == .OK { model.addFiles(panel.urls) } }
}
