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
    @Published var reviewPresented = false
    @Published var useTesseract = true
    @Published var includePDF = false
    @Published var nativeTextOnly = false
    @Published var docxBusy = false
    private var bridge: CoreProcessBridge?
    private var startedAt: Date?
    private var preflightGeneration = 0

    var elapsed: String { guard let startedAt else { return "" }; return "Elapsed \(Int(Date().timeIntervalSince(startedAt)))s" }
    func addFiles(_ urls: [URL]) { sourceURLs = urls; ready = false; result = [:]; errorText = ""; preflight() }
    func preflight() {
        preflightGeneration += 1
        let generation = preflightGeneration
        presentation.state = .preflighting; errorText = ""; ready = false
        let client = CoreProcessBridge(configuration: CoreConfiguration(scriptPath: corePath)); bridge = client
        let operation = nativeTextOnly ? "native-text" : "run"
        do { try client.start(arguments: ["preflight", "--operation", operation], event: { [weak self] event in Task { @MainActor in self?.receivePreflight(event, generation: generation) } }, stderr: { [weak self] text in Task { @MainActor in guard self?.preflightGeneration == generation else { return }; self?.receiveStderr(text) } }, finished: { [weak self] status in Task { @MainActor in guard let self, self.preflightGeneration == generation else { return }; if status != 0 && self.errorText.isEmpty { self.errorText = "Preflight failed (exit \(status))." } } }) }
        catch { errorText = error.localizedDescription; presentation.state = .failed }
    }
    private func receivePreflight(_ event: MachineEvent, generation: Int) { Task { @MainActor in
        guard preflightGeneration == generation else { return }
        presentation.consume(event)
        if event.eventType == .jobCompleted { ready = event.details.object?["ok"]?.bool == true; presentation.state = ready ? .ready : .failed }
        if event.eventType == .jobFailed { errorText = "\(event.code): \(event.message)" }
    } }
    func start() {
        guard !sourceURLs.isEmpty, ready else { return }
        presentation = JobPresentation(state: .running); errorText = ""; warningText = ""; startedAt = Date()
        let coreOptions = nativeTextOnly ? ["--native-text-only"] : ((useTesseract ? ["--ocr", "tesseract"] : []) + ["--no-qa"] + (includePDF ? [] : ["--no-pdf"]) + ["--raw-ocr"])
        let options = ["run"] + sourceURLs.map(\.path) + ["--"] + coreOptions
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
        if status == 0, let outputURL {
            nativeTextOnly ? buildDocx(outputURL, openWhenReady: false) : inspect(outputURL)
        } else if presentation.state != .failed { presentation.state = .failed; errorText = "processing_failure: Core exited with \(status)." }
    }
    func cancel() { guard bridge?.isRunning == true else { return }; presentation.state = .cancelling; bridge?.cancel() }
    func inspect(_ url: URL) {
        outputURL = url; UserDefaults.standard.set(url.path, forKey: "booktext.last.output")
        let client = CoreProcessBridge(configuration: CoreConfiguration(scriptPath: corePath)); bridge = client
        do { try client.start(arguments: ["inspect", url.path], event: { [weak self] event in Task { @MainActor in
            self?.presentation.consume(event); if event.eventType == .jobCompleted { self?.result = event.details.object ?? [:]; self?.presentation.state = .completed }
        } }, stderr: { [weak self] text in Task { @MainActor in self?.receiveStderr(text) } }, finished: { _ in }) } catch { errorText = error.localizedDescription }
    }
    /// Builds from existing artifacts only.  This does not enter the OCR run
    /// path; Core validates the CDM and export manifest before a file is open.
    func exportAndOpenDocx() {
        guard let outputURL, !docxBusy else { return }
        docxBusy = true
        warningText = "Preparing DOCX from existing BookText artifacts…"
        buildDocx(outputURL, openWhenReady: true)
    }
    private func buildDocx(_ url: URL, openWhenReady: Bool) {
        let client = CoreProcessBridge(configuration: CoreConfiguration(scriptPath: corePath)); bridge = client
        do { try client.start(arguments: ["cdm", url.path], event: { [weak self] event in Task { @MainActor in
            guard let self else { return }
            if event.eventType == .jobCompleted { self.exportDocx(url, openWhenReady: openWhenReady) }
            if event.eventType == .jobFailed { self.docxBusy = false; self.presentation.state = .failed; self.errorText = "cdm_failure: \(event.message)" }
        } }, stderr: { [weak self] text in Task { @MainActor in self?.receiveStderr(text) } }, finished: { _ in }) } catch { docxBusy = false; presentation.state = .failed; errorText = error.localizedDescription }
    }
    private func exportDocx(_ url: URL, openWhenReady: Bool) {
        let client = CoreProcessBridge(configuration: CoreConfiguration(scriptPath: corePath)); bridge = client
        do { try client.start(arguments: ["export", url.path, "--format", "docx"], event: { [weak self] event in Task { @MainActor in
            guard let self else { return }
            if event.eventType == .jobCompleted {
                self.docxBusy = false
                let entry = event.details.object?["result"]?.object?["generated"]?.object?["docx"]?.object
                let relative = entry?["named_deliverable"]?.string ?? entry?["path"]?.string
                if openWhenReady, let relative, let destination = self.safeOutputFile(url, relative) {
                    NSWorkspace.shared.open(destination)
                    self.warningText = "DOCX created and opened: \(destination.lastPathComponent)"
                } else if openWhenReady {
                    self.warningText = "DOCX was created, but its output path was not safe to open automatically."
                }
                self.inspect(url)
            }
            if event.eventType == .jobFailed { self.docxBusy = false; self.presentation.state = .failed; self.errorText = "export_failure: \(event.message)" }
        } }, stderr: { [weak self] text in Task { @MainActor in self?.receiveStderr(text) } }, finished: { _ in }) } catch { docxBusy = false; presentation.state = .failed; errorText = error.localizedDescription }
    }
    private func safeOutputFile(_ root: URL, _ relative: String) -> URL? {
        guard !relative.isEmpty, !relative.contains("..") else { return nil }
        let base = root.standardizedFileURL
        let candidate = base.appendingPathComponent(relative).standardizedFileURL
        guard candidate.path.hasPrefix(base.path + "/"), candidate.pathExtension.lowercased() == "docx", FileManager.default.fileExists(atPath: candidate.path) else { return nil }
        return candidate
    }
    func reveal() { if let outputURL { NSWorkspace.shared.activateFileViewerSelecting([outputURL]) } }
    func openPrimary() { guard let outputURL, let artifacts = result["artifacts"]?.object else { return }; let key = artifacts["clean_text"]?.object?["exists"]?.bool == true ? "clean_text" : "markdown"; if let relative = artifacts[key]?.object?["path"]?.string { NSWorkspace.shared.open(outputURL.appendingPathComponent(relative)) } }
    func openReview() { guard outputURL != nil else { return }; reviewPresented = true }
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
            HStack { Button("Choose File(s)") { chooseFiles() }; Button("Open Existing Output") { chooseOutput() }; Toggle("Use Tesseract (quick local run)", isOn: $model.useTesseract).disabled(model.nativeTextOnly); Toggle("Create searchable PDF", isOn: $model.includePDF).disabled(model.nativeTextOnly) }
            Toggle("Native PDF text only → DOCX (do not OCR images or figures)", isOn: $model.nativeTextOnly).onChange(of: model.nativeTextOnly) { _, _ in model.preflight() }
        }.padding(10).frame(maxWidth: .infinity).background(dragging ? Color.accentColor.opacity(0.15) : .clear).onDrop(of: [.fileURL], isTargeted: $dragging) { providers in
            guard let provider = providers.first else { return false }; provider.loadItem(forTypeIdentifier: UTType.fileURL.identifier, options: nil) { item, _ in if let data = item as? Data, let url = URL(dataRepresentation: data, relativeTo: nil) { Task { @MainActor in model.addFiles([url]) } } }; return true
        } }
        status
        Spacer()
    }.padding(24).sheet(isPresented: $model.reviewPresented) { if let output = model.outputURL { NativeReviewView(outputURL: output, configuration: CoreConfiguration(scriptPath: model.corePath)) } } }
    @ViewBuilder var status: some View {
        switch model.presentation.state {
        case .idle: Text("Choose an input to begin.")
        case .preflighting: ProgressView("Checking local Core prerequisites…")
        case .ready: Button("Start BookText") { model.start() }.buttonStyle(.borderedProminent).disabled(model.sourceURLs.isEmpty)
        case .running, .cancelling: VStack(alignment: .leading) { ProgressView(value: Double(model.presentation.current ?? 0), total: Double(max(model.presentation.total ?? 1, 1))); Text("\(model.presentation.phase ?? "working")  page \(model.presentation.page.map(String.init) ?? "—")  \(model.elapsed)"); Button(model.presentation.state == .cancelling ? "Cancelling…" : "Cancel") { model.cancel() }.disabled(model.presentation.state == .cancelling) }
        case .cancelled: Label("Cancelled — Core cleanup completed.", systemImage: "xmark.circle").foregroundStyle(.orange)
        case .completed: VStack(alignment: .leading) { Label("Completed", systemImage: "checkmark.circle.fill").foregroundStyle(.green); if let url = model.outputURL { Text(url.path).textSelection(.enabled) }; resultSummary; HStack { Button("Reveal in Finder") { model.reveal() }; Button("Open Primary Result") { model.openPrimary() }; Button("Open Review") { model.openReview() }; Button(model.docxBusy ? "Building DOCX…" : "Export & Open DOCX") { model.exportAndOpenDocx() }.disabled(model.docxBusy) } }
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
    private func chooseOutput() { let panel = NSOpenPanel(); panel.allowsMultipleSelection = false; panel.canChooseFiles = false; panel.canChooseDirectories = true; if panel.runModal() == .OK, let url = panel.url { model.inspect(url) } }
}
