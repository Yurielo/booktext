import SwiftUI
import AppKit
import UniformTypeIdentifiers
import BookTextCore

@main struct BookTextApp: App {
    @StateObject private var model = BookTextModel()
    var body: some Scene {
        WindowGroup { ContentView().environmentObject(model).frame(minWidth: 760, minHeight: 560) }
        Settings { SettingsView().environmentObject(model).frame(width: 620, height: 520) }
    }
}

@MainActor final class BookTextModel: ObservableObject {
    @Published var corePath = UserDefaults.standard.string(forKey: "booktext.core.path") ?? CoreConfiguration.defaultValue.scriptPath { didSet { UserDefaults.standard.set(corePath, forKey: "booktext.core.path") } }
    @Published var sourceURLs: [URL] = []
    @Published var presentation = JobPresentation()
    @Published var ready = false
    @Published var errorText = ""
    @Published var warningText = ""
    @Published private(set) var diagnostics = ""
    @Published var result: [String: JSONValue] = [:]
    @Published var outputURL: URL?
    @Published var reviewPresented = false
    @Published var finalPresented = false
    @Published var finalSection: FinalBookSection = .preview
    @Published var useTesseract = true
    @Published var includePDF = false
    @Published var nativeTextOnly = false
    @Published var cleanupBusy = false
    @Published private(set) var recentOutputPaths: [String] = []
    @Published private(set) var resumedRun = false
    @Published private(set) var buildingFinalBook = false
    private var bridge: CoreProcessBridge?
    private var startedAt: Date?
    private var preflightGeneration = 0

    var elapsed: String { guard let startedAt else { return "" }; return "\(Int(Date().timeIntervalSince(startedAt))) sec" }
    var documentName: String {
        if let source = sourceURLs.first { return source.deletingPathExtension().lastPathComponent }
        if let outputURL { let name = outputURL.lastPathComponent; return name.hasSuffix("_book") ? String(name.dropLast(5)) : name }
        return "Book"
    }
    var snapshot: DocumentPresentationSnapshot {
        DocumentPresentationMapper.snapshot(sourceSelected: !sourceURLs.isEmpty, outputSelected: outputURL != nil && !result.isEmpty, jobState: presentation.state, inspection: result)
    }

    func loadRecents() {
        let stored = UserDefaults.standard.stringArray(forKey: "booktext.recent.outputs") ?? []
        let legacy = UserDefaults.standard.string(forKey: "booktext.last.output")
        recentOutputPaths = RecentOutputList.normalized(stored, adding: legacy).filter { path in
            var directory: ObjCBool = false
            return FileManager.default.fileExists(atPath: path, isDirectory: &directory) && directory.boolValue
        }
        UserDefaults.standard.set(recentOutputPaths, forKey: "booktext.recent.outputs")
    }
    func clearRecents() {
        recentOutputPaths = []
        UserDefaults.standard.removeObject(forKey: "booktext.recent.outputs")
        UserDefaults.standard.removeObject(forKey: "booktext.last.output")
    }
    private func rememberOutput(_ url: URL) {
        recentOutputPaths = RecentOutputList.normalized(recentOutputPaths, adding: url.path)
        UserDefaults.standard.set(recentOutputPaths, forKey: "booktext.recent.outputs")
        UserDefaults.standard.set(url.path, forKey: "booktext.last.output")
    }
    func addFiles(_ urls: [URL]) {
        sourceURLs = urls; outputURL = nil; result = [:]; ready = false; resumedRun = false
        presentation = JobPresentation(); errorText = ""; warningText = ""; preflight()
    }
    func resetToChooser() {
        sourceURLs = []; outputURL = nil; result = [:]; presentation = JobPresentation(); ready = false
        errorText = ""; warningText = ""; resumedRun = false
    }
    func preflight() {
        preflightGeneration += 1
        let generation = preflightGeneration
        presentation.state = .preflighting; errorText = ""; ready = false
        let client = CoreProcessBridge(configuration: CoreConfiguration(scriptPath: corePath)); bridge = client
        let operation = nativeTextOnly ? "native-text" : "run"
        do { try client.start(arguments: ["preflight", "--operation", operation], event: { [weak self] event in Task { @MainActor in self?.receivePreflight(event, generation: generation) } }, stderr: { [weak self] text in Task { @MainActor in guard self?.preflightGeneration == generation else { return }; self?.receiveStderr(text) } }, finished: { [weak self] status in Task { @MainActor in
            guard let self, self.preflightGeneration == generation else { return }
            if status != 0 && self.errorText.isEmpty { self.errorText = "BookText requirements could not be verified (exit \(status))."; self.presentation.state = .failed }
        } }) } catch { errorText = error.localizedDescription; presentation.state = .failed }
    }
    private func receivePreflight(_ event: MachineEvent, generation: Int) {
        guard preflightGeneration == generation else { return }
        presentation.consume(event)
        if event.eventType == .jobCompleted {
            ready = event.details.object?["ok"]?.bool == true; presentation.state = ready ? .ready : .failed
            if !ready { errorText = "BookText is not ready for this operation. Open Settings for details." }
        }
        if event.eventType == .jobFailed { errorText = "\(event.code): \(event.message)" }
    }
    func start() {
        guard !sourceURLs.isEmpty, ready else { return }
        presentation = JobPresentation(state: .running); errorText = ""; warningText = ""; resumedRun = false; startedAt = Date()
        let coreOptions = nativeTextOnly ? ["--native-text-only"] : ((useTesseract ? ["--ocr", "tesseract"] : []) + ["--no-qa"] + (includePDF ? [] : ["--no-pdf"]) + ["--raw-ocr"])
        let arguments = ["run"] + sourceURLs.map(\.path) + ["--"] + coreOptions
        let client = CoreProcessBridge(configuration: CoreConfiguration(scriptPath: corePath)); bridge = client
        do { try client.start(arguments: arguments, event: { [weak self] event in Task { @MainActor in self?.receiveRun(event) } }, stderr: { [weak self] text in Task { @MainActor in self?.receiveStderr(text) } }, finished: { [weak self] status in Task { @MainActor in self?.finishRun(status) } }) } catch { errorText = error.localizedDescription; presentation.state = .failed }
    }
    private func receiveRun(_ event: MachineEvent) {
        presentation.consume(event)
        if event.eventType == .jobResumed { resumedRun = true }
        if event.eventType == .warning { warningText += (warningText.isEmpty ? "" : "\n") + event.message }
        if let path = event.details.object?["output_dir"]?.string { outputURL = URL(fileURLWithPath: path); rememberOutput(outputURL!) }
        if event.eventType == .jobFailed { errorText = "\(event.code): \(event.message)" }
        if event.eventType == .jobCancelled { presentation.state = .cancelled }
    }
    private func receiveStderr(_ text: String) { if !text.isEmpty { diagnostics += text } }
    private func finishRun(_ status: Int32) {
        guard presentation.state != .cancelled else { return }
        if status == 0, let outputURL { inspect(outputURL) }
        else if presentation.state != .failed { presentation.state = .failed; errorText = "processing_failure: BookText Core exited with \(status)." }
    }
    func cancel() { guard bridge?.isRunning == true else { return }; presentation.state = .cancelling; bridge?.cancel() }
    func inspect(_ url: URL) {
        outputURL = url.standardizedFileURL; sourceURLs = []; result = [:]; errorText = ""
        presentation = JobPresentation(state: .running, phase: "prepare"); rememberOutput(url)
        let client = CoreProcessBridge(configuration: CoreConfiguration(scriptPath: corePath)); bridge = client
        do { try client.start(arguments: ["inspect", url.path], event: { [weak self] event in Task { @MainActor in
            guard let self else { return }; self.presentation.consume(event)
            if event.eventType == .jobCompleted { self.result = event.details.object ?? [:]; self.presentation.state = .completed }
            if event.eventType == .jobFailed { self.errorText = "\(event.code): \(event.message)" }
        } }, stderr: { [weak self] text in Task { @MainActor in self?.receiveStderr(text) } }, finished: { [weak self] status in Task { @MainActor in
            guard let self else { return }
            if status != 0 && self.presentation.state != .failed { self.presentation.state = .failed; self.errorText = "Unable to open this BookText output." }
        } }) } catch { presentation.state = .failed; errorText = error.localizedDescription }
    }
    func createFinalBook() {
        guard let outputURL, !buildingFinalBook else { return }
        buildingFinalBook = true; presentation = JobPresentation(state: .running, phase: "final_build")
        runCore(["final-build", outputURL.path]) { [weak self] success in
            guard let self else { return }
            if success { self.presentation.phase = "cdm"; self.runCore(["cdm", outputURL.path]) { [weak self] cdmSuccess in guard let self else { return }; self.buildingFinalBook = false; if cdmSuccess { self.inspect(outputURL) } } }
            else { self.buildingFinalBook = false }
        }
    }
    private func runCore(_ arguments: [String], completion: @escaping @MainActor (Bool) -> Void) {
        let client = CoreProcessBridge(configuration: CoreConfiguration(scriptPath: corePath)); bridge = client
        do { try client.start(arguments: arguments, event: { [weak self] event in Task { @MainActor in
            guard let self else { return }
            self.presentation.consume(event)
            if event.eventType == .jobCompleted { completion(true) }
            if event.eventType == .jobFailed { self.errorText = "\(event.code): \(event.message)"; completion(false) }
        } }, stderr: { [weak self] text in Task { @MainActor in self?.receiveStderr(text) } }, finished: { status in
            if status != 0 { Task { @MainActor in completion(false) } }
        }) } catch { errorText = error.localizedDescription; presentation.state = .failed; completion(false) }
    }
    func openReview() { if outputURL != nil { reviewPresented = true } }
    func showFinal(_ section: FinalBookSection) { guard outputURL != nil else { return }; finalSection = section; finalPresented = true }
    func reveal() { if let outputURL { NSWorkspace.shared.activateFileViewerSelecting([outputURL]) } }
    func openRecognizedText() {
        guard let outputURL, let artifacts = result["artifacts"]?.object else { return }
        let key = artifacts["clean_text"]?.object?["exists"]?.bool == true ? "clean_text" : "markdown"
        if let relative = artifacts[key]?.object?["path"]?.string { NSWorkspace.shared.open(outputURL.appendingPathComponent(relative)) }
    }
    /// Core owns cleanup policy. Swift never classifies disposable artifacts.
    func cleanTemporaryFiles() {
        guard let outputURL, !cleanupBusy else { return }; cleanupBusy = true
        let client = CoreProcessBridge(configuration: CoreConfiguration(scriptPath: corePath)); bridge = client
        do { try client.start(arguments: ["cleanup", outputURL.path], event: { [weak self] event in Task { @MainActor in
            guard let self else { return }
            if event.eventType == .cleanupCompleted || event.eventType == .jobCompleted { let detail = event.details.object?["result"]?.object ?? event.details.object ?? [:]; self.warningText = "Temporary storage cleaned: \(detail["bytes_deleted"]?.integer ?? 0) bytes."; self.cleanupBusy = false }
            if event.eventType == .cleanupWarning { self.warningText = event.message; self.cleanupBusy = false }
            if event.eventType == .jobFailed { self.errorText = "cleanup_failure: \(event.message)"; self.cleanupBusy = false }
        } }, stderr: { [weak self] text in Task { @MainActor in self?.receiveStderr(text) } }, finished: { [weak self] _ in Task { @MainActor in self?.cleanupBusy = false } }) } catch { cleanupBusy = false; errorText = error.localizedDescription }
    }
}

struct ContentView: View {
    @EnvironmentObject private var model: BookTextModel
    @State private var dragging = false
    @State private var showDetails = false
    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            header
            Group { content }.frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
            if !model.warningText.isEmpty { Label(model.warningText, systemImage: "exclamationmark.triangle").font(.caption).foregroundStyle(.orange).lineLimit(3).textSelection(.enabled) }
        }
        .padding(28).task { model.loadRecents() }
        .sheet(isPresented: $model.reviewPresented, onDismiss: refreshCurrentOutput) { if let output = model.outputURL { NativeReviewView(outputURL: output, configuration: CoreConfiguration(scriptPath: model.corePath)) } }
        .sheet(isPresented: $model.finalPresented, onDismiss: refreshCurrentOutput) { if let output = model.outputURL { FinalBookView(outputURL: output, configuration: CoreConfiguration(scriptPath: model.corePath), initialSection: model.finalSection) } }
    }
    private var header: some View {
        HStack(spacing: 14) {
            Image(nsImage: NSApplication.shared.applicationIconImage).resizable().scaledToFit().frame(width: 48, height: 48).accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) { Text("BookText").font(.largeTitle.bold()).accessibilityAddTraits(.isHeader); Text("Turn a document into a reviewable, ready-to-use book.").foregroundStyle(.secondary) }
            Spacer(); SettingsLink { Label("Settings", systemImage: "gearshape") }.labelStyle(.iconOnly).help("Settings and diagnostics")
        }
    }
    @ViewBuilder private var content: some View {
        switch model.snapshot.state {
        case .noDocument: chooser
        case .readyToProcess: selectedDocument
        case .processing, .translating: processing
        case .interruptedResumable: interrupted
        case .reviewRequired, .readyForFinalBuild, .bookReady, .translationReady: completedDocument
        case .failed: failed
        }
    }
    private var chooser: some View {
        VStack(spacing: 20) {
            VStack(spacing: 14) {
                Image(systemName: "doc.badge.plus").font(.system(size: 42)).foregroundStyle(.tint)
                Text("Choose a document").font(.title2.bold())
                Text("Drop a PDF, DjVu, image, or supported document here.").foregroundStyle(.secondary)
                Button("Choose File", action: chooseFiles).buttonStyle(.borderedProminent).controlSize(.large)
                Button("Open Existing BookText Output…", action: chooseOutput).buttonStyle(.link)
            }
            .padding(32).frame(maxWidth: .infinity, minHeight: 230)
            .background(dragging ? Color.accentColor.opacity(0.14) : Color.secondary.opacity(0.06)).clipShape(RoundedRectangle(cornerRadius: 16))
            .overlay(RoundedRectangle(cornerRadius: 16).strokeBorder(dragging ? Color.accentColor : Color.secondary.opacity(0.25), style: StrokeStyle(lineWidth: 1.5, dash: [7])))
            .onDrop(of: [.fileURL], isTargeted: $dragging, perform: acceptDrop).accessibilityLabel("Drop a document here or choose a file")
            recentDocuments
        }
    }
    @ViewBuilder private var recentDocuments: some View {
        if !model.recentOutputPaths.isEmpty {
            VStack(alignment: .leading, spacing: 8) {
                Text("Recent documents").font(.headline)
                ForEach(model.recentOutputPaths.prefix(5), id: \.self) { path in
                    Button { model.inspect(URL(fileURLWithPath: path)) } label: {
                        HStack { Image(systemName: "book.closed"); Text(displayName(for: path)).lineLimit(1); Spacer(); Text("Open existing output").font(.caption).foregroundStyle(.secondary); Image(systemName: "chevron.right").foregroundStyle(.tertiary) }.contentShape(Rectangle())
                    }.buttonStyle(.plain).padding(.vertical, 7)
                }
            }.padding(.horizontal, 4)
        }
    }
    private var selectedDocument: some View {
        VStack(alignment: .leading, spacing: 18) {
            documentCard(status: model.ready ? "Ready" : "Checking requirements…", symbol: model.ready ? "checkmark.circle.fill" : "hourglass")
            if model.presentation.state == .preflighting { ProgressView().controlSize(.small) }
            HStack { Button("Start Processing", action: model.start).buttonStyle(.borderedProminent).controlSize(.large).disabled(!model.ready); Button("Choose Another Document", action: chooseFiles); SettingsLink { Text("Options…") } }
        }.frame(maxWidth: 680, alignment: .leading)
    }
    private var processing: some View {
        VStack(alignment: .leading, spacing: 18) {
            documentCard(status: DocumentPresentationMapper.phaseLabel(model.presentation.phase), symbol: "gearshape.2")
            if model.resumedRun { Label("Resuming previous processing. Completed work will not be repeated.", systemImage: "arrow.clockwise.circle").foregroundStyle(.blue) }
            ProgressView(value: Double(model.presentation.current ?? 0), total: Double(max(model.presentation.total ?? 1, 1)))
            HStack { Text(progressDescription).foregroundStyle(.secondary); Spacer(); if !model.elapsed.isEmpty { Text(model.elapsed).monospacedDigit().foregroundStyle(.secondary) } }
            Button(model.presentation.state == .cancelling ? "Stopping…" : "Stop", action: model.cancel).disabled(model.presentation.state == .cancelling)
            DisclosureGroup("Details", isExpanded: $showDetails) { VStack(alignment: .leading, spacing: 5) { Text("Phase: \(model.presentation.phase ?? "—")"); Text("Job: \(model.presentation.jobID ?? "—")"); if !model.diagnostics.isEmpty { Text(model.diagnostics).lineLimit(8).textSelection(.enabled) } }.font(.caption.monospaced()).foregroundStyle(.secondary).padding(.top, 6) }
        }.frame(maxWidth: 720, alignment: .leading)
    }
    private var interrupted: some View {
        VStack(alignment: .leading, spacing: 16) { documentCard(status: "Processing paused", symbol: "pause.circle"); Text("Completed work is saved. Choose the same source to resume without repeating finished pages.").foregroundStyle(.secondary); Button("Choose Source to Resume…", action: chooseFiles).buttonStyle(.borderedProminent); Button("Back to Documents", action: model.resetToChooser) }
    }
    private var completedDocument: some View {
        VStack(alignment: .leading, spacing: 18) {
            documentCard(status: lifecycleTitle, symbol: lifecycleSymbol); statusSummary; primaryDocumentAction
            HStack(spacing: 12) {
                if model.snapshot.state == .bookReady || model.snapshot.state == .translationReady { Button("Translate") { model.showFinal(.translation) }; Button("Export…") { model.showFinal(.export) } }
                Button("Reveal in Finder", action: model.reveal)
                Menu("More") { Button("Open Recognized Text", action: model.openRecognizedText); Button("Open Another Document…", action: model.resetToChooser); SettingsLink { Text("Settings and Diagnostics…") } }
            }
        }.frame(maxWidth: 760, alignment: .leading)
    }
    @ViewBuilder private var primaryDocumentAction: some View {
        switch model.snapshot.primaryAction {
        case .reviewItems: Button("Review \(model.snapshot.unresolvedHigh + model.snapshot.unresolvedMedium) Items", action: model.openReview).buttonStyle(.borderedProminent).controlSize(.large)
        case .createFinalBook: Button(model.buildingFinalBook ? "Creating Final Book…" : "Create Final Book", action: model.createFinalBook).buttonStyle(.borderedProminent).controlSize(.large).disabled(model.buildingFinalBook)
        case .openBook: Button("Open Book") { model.showFinal(.preview) }.buttonStyle(.borderedProminent).controlSize(.large)
        case .openTranslation: HStack { Button("Open Translation") { model.showFinal(.translation) }.buttonStyle(.borderedProminent).controlSize(.large); Button("Open Book") { model.showFinal(.preview) } }
        default: EmptyView()
        }
    }
    private var failed: some View {
        VStack(alignment: .leading, spacing: 14) { Label("BookText needs attention", systemImage: "exclamationmark.triangle.fill").font(.title2.bold()).foregroundStyle(.red); Text(model.errorText).textSelection(.enabled); HStack { if !model.sourceURLs.isEmpty { Button("Try Again", action: model.preflight).buttonStyle(.borderedProminent) }; SettingsLink { Text("Open Settings and Diagnostics") }; Button("Choose Another Document", action: model.resetToChooser) } }
    }
    private func documentCard(status: String, symbol: String) -> some View {
        HStack(spacing: 14) { Image(systemName: "doc.text").font(.system(size: 30)).foregroundStyle(.tint); VStack(alignment: .leading, spacing: 4) { Text(model.documentName).font(.title2.bold()).lineLimit(2); HStack { Label(status, systemImage: symbol); if let pages = model.snapshot.pageCount { Text("· \(pages) pages") }; if let source = model.sourceURLs.first { Text("· \(source.pathExtension.uppercased())") } }.font(.subheadline).foregroundStyle(.secondary) } }
            .padding(18).frame(maxWidth: .infinity, alignment: .leading).background(Color.secondary.opacity(0.07)).clipShape(RoundedRectangle(cornerRadius: 12))
    }
    private var statusSummary: some View {
        VStack(alignment: .leading, spacing: 6) {
            if model.snapshot.state == .reviewRequired { Text("Recognition needs your review before the final book is ready."); Text("HIGH \(model.snapshot.unresolvedHigh) · MEDIUM \(model.snapshot.unresolvedMedium)").font(.subheadline.weight(.semibold)).foregroundStyle(.orange) }
            else if model.snapshot.state == .readyForFinalBuild { Text("Review is complete. Create the final book to prepare preview and export.") }
            else { Text("The book is ready to open, translate, or export."); if model.snapshot.translationAvailable { Label("Translation available", systemImage: "character.book.closed") } }
        }.foregroundStyle(.secondary)
    }
    private var lifecycleTitle: String { switch model.snapshot.state { case .reviewRequired: "Review required"; case .readyForFinalBuild: "Ready for final book"; case .translationReady: "Book and translation ready"; default: "Book ready" } }
    private var lifecycleSymbol: String { model.snapshot.state == .reviewRequired ? "exclamationmark.circle.fill" : "checkmark.circle.fill" }
    private var progressDescription: String { if let current = model.presentation.current, let total = model.presentation.total { return "\(current) of \(total)" + (model.presentation.page.map { " · page \($0)" } ?? "") }; return model.presentation.page.map { "Page \($0)" } ?? "Working…" }
    private func refreshCurrentOutput() { if let output = model.outputURL { model.inspect(output) } }
    private func displayName(for path: String) -> String { let name = URL(fileURLWithPath: path).lastPathComponent; return name.hasSuffix("_book") ? String(name.dropLast(5)) : name }
    private func chooseFiles() {
        let panel = NSOpenPanel(); panel.allowsMultipleSelection = true; panel.canChooseDirectories = false
        panel.allowedContentTypes = [.pdf, .image, .plainText, .rtf, UTType(filenameExtension: "djvu") ?? .data, UTType(filenameExtension: "djv") ?? .data, UTType(filenameExtension: "docx") ?? .data]
        if panel.runModal() == .OK { model.addFiles(panel.urls) }
    }
    private func chooseOutput() { let panel = NSOpenPanel(); panel.allowsMultipleSelection = false; panel.canChooseFiles = false; panel.canChooseDirectories = true; panel.prompt = "Open Book"; if panel.runModal() == .OK, let url = panel.url { model.inspect(url) } }
    private func acceptDrop(_ providers: [NSItemProvider]) -> Bool {
        guard let provider = providers.first else { return false }
        provider.loadItem(forTypeIdentifier: UTType.fileURL.identifier, options: nil) { item, _ in if let data = item as? Data, let url = URL(dataRepresentation: data, relativeTo: nil) { Task { @MainActor in model.addFiles([url]) } } }
        return true
    }
}

struct SettingsView: View {
    @EnvironmentObject private var model: BookTextModel
    @State private var showDiagnostics = false
    var body: some View {
        Form {
            Section("General") { LabeledContent("Recent documents") { Text("Up to 5 local outputs") }; Button("Clear Recent Documents", action: model.clearRecents).disabled(model.recentOutputPaths.isEmpty) }
            Section("Processing") { Toggle("Fast local recognition (Tesseract)", isOn: $model.useTesseract).disabled(model.nativeTextOnly); Toggle("Create searchable PDF", isOn: $model.includePDF).disabled(model.nativeTextOnly); Toggle("Use embedded PDF text only", isOn: $model.nativeTextOnly); Text("Automatic processing and automatic chunk sizing remain the defaults.").font(.caption).foregroundStyle(.secondary) }
            Section("Advanced and Diagnostics") {
                DisclosureGroup("BookText Core", isExpanded: $showDiagnostics) { TextField("Path to booktext.py", text: $model.corePath).textFieldStyle(.roundedBorder); HStack { Button("Check Requirements", action: model.preflight); Text("Machine protocol \(machineProtocolVersion)").foregroundStyle(.secondary) }; if !model.diagnostics.isEmpty { ScrollView { Text(model.diagnostics).font(.caption.monospaced()).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading) }.frame(maxHeight: 100) } }
                Button(model.cleanupBusy ? "Cleaning…" : "Clean Temporary Files", action: model.cleanTemporaryFiles).disabled(model.outputURL == nil || model.cleanupBusy)
                Text("Cleanup policy is validated and executed by BookText Core for the current output.").font(.caption).foregroundStyle(.secondary)
            }
        }.formStyle(.grouped).padding().onChange(of: model.nativeTextOnly) { _, _ in model.preflight() }
    }
}
