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
    @Published var includePDF = false
    @Published var workflowReviewPolicy = "skip_human_review"
    @Published var workflowTranslate = false
    @Published var workflowTranslationSource = "en"
    @Published var workflowTranslationModel = "qwen3:8b"
    @Published var workflowExportFormats: Set<String> = ["docx"]
    @Published var cleanupBusy = false
    @Published private(set) var recentOutputPaths: [String] = []
    @Published private(set) var projects: [ProjectHistoryEntry] = []
    @Published private(set) var historyWarning = ""
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
        loadProjectHistory(knownOutputs: recentOutputPaths)
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
    private func loadProjectHistory(knownOutputs: [String] = []) {
        let client = CoreProcessBridge(configuration: CoreConfiguration(scriptPath: corePath))
        let arguments = ["project-history"] + knownOutputs.flatMap { ["--known-output", $0] }
        do { try client.start(arguments: arguments, event: { [weak self] event in Task { @MainActor in
            guard let self, event.eventType == .jobCompleted else { return }
            let details = event.details.object ?? [:]
            self.projects = details["projects"]?.values?.compactMap(ProjectHistoryEntry.init) ?? []
            self.historyWarning = details["warnings"]?.values?.compactMap(\.string).joined(separator: "\n") ?? ""
        } }, stderr: { _ in }, finished: { _ in }) } catch { historyWarning = "Project history is unavailable: \(error.localizedDescription)" }
    }
    func removeProject(_ project: ProjectHistoryEntry) {
        let client = CoreProcessBridge(configuration: CoreConfiguration(scriptPath: corePath))
        do { try client.start(arguments: ["project-remove", project.id], event: { [weak self] event in Task { @MainActor in
            guard event.eventType == .jobCompleted else { return }
            self?.projects.removeAll { $0.id == project.id }
        } }, stderr: { _ in }, finished: { _ in }) } catch { historyWarning = "Could not remove project from history: \(error.localizedDescription)" }
    }
    func openProject(_ project: ProjectHistoryEntry) {
        guard project.reopenAvailable else { errorText = project.detailForUnavailable; presentation.state = .failed; return }
        inspect(URL(fileURLWithPath: project.outputPath))
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
        let operation = "run"
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
    private func pipelineDefinitionJSON(coreOptions: [String]) -> String? {
        var stages: [[String: Any]] = [
            ["stage_id": "process", "type": "PROCESS", "config": ["core_args": coreOptions]],
            ["stage_id": "review", "type": "REVIEW_POLICY", "config": ["policy": workflowReviewPolicy]],
            ["stage_id": "final-build", "type": "FINAL_BUILD", "config": [:]],
            ["stage_id": "cdm", "type": "CDM", "config": [:]],
        ]
        if workflowTranslate {
            stages.append(["stage_id": "translate", "type": "TRANSLATE", "config": ["source_language": workflowTranslationSource, "target_language": workflowTranslationSource == "en" ? "ru" : "en", "provider": "ollama", "model": workflowTranslationModel]])
        }
        if !workflowExportFormats.isEmpty {
            stages.append(["stage_id": "export", "type": "EXPORT", "config": ["formats": ExportFormatSelection.ordered(workflowExportFormats), "document": workflowTranslate ? "TRANSLATION" : "ORIGINAL"]])
        }
        guard let data = try? JSONSerialization.data(withJSONObject: ["schema_version": "1.0", "stages": stages]), let value = String(data: data, encoding: .utf8) else { return nil }
        return value
    }
    func start() {
        guard !sourceURLs.isEmpty, ready else { return }
        presentation = JobPresentation(state: .running); errorText = ""; warningText = ""; resumedRun = false; startedAt = Date()
        let coreOptions = AutomaticProcessingOptions.arguments(includeSearchablePDF: includePDF)
        guard let definition = pipelineDefinitionJSON(coreOptions: coreOptions), let source = sourceURLs.first else { errorText = "workflow configuration is invalid"; presentation.state = .failed; return }
        let client = CoreProcessBridge(configuration: CoreConfiguration(scriptPath: corePath)); bridge = client
        do { try client.start(arguments: ["pipeline-create", "--definition-json", definition, source.path], event: { [weak self] event in Task { @MainActor in
            guard let self else { return }; self.presentation.consume(event)
            if event.eventType == .jobCompleted, let path = event.details.object?["output_dir"]?.string { self.outputURL = URL(fileURLWithPath: path); self.rememberOutput(self.outputURL!); self.runPipeline(resume: false) }
            if event.eventType == .jobFailed { self.errorText = "\(event.code): \(event.message)" }
        } }, stderr: { [weak self] text in Task { @MainActor in self?.receiveStderr(text) } }, finished: { [weak self] status in Task { @MainActor in if status != 0 { self?.presentation.state = .failed; self?.errorText = "workflow configuration failed" } } }) } catch { errorText = error.localizedDescription; presentation.state = .failed }
    }
    func runPipeline(resume: Bool) {
        guard let outputURL else { return }
        let client = CoreProcessBridge(configuration: CoreConfiguration(scriptPath: corePath)); bridge = client
        do { try client.start(arguments: [resume ? "pipeline-resume" : "pipeline-start", outputURL.path], event: { [weak self] event in Task { @MainActor in self?.receiveRun(event) } }, stderr: { [weak self] text in Task { @MainActor in self?.receiveStderr(text) } }, finished: { [weak self] status in Task { @MainActor in self?.finishRun(status) } }) } catch { errorText = error.localizedDescription; presentation.state = .failed }
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
        do { try client.start(arguments: ["inspect", url.path, "--record-activity"], event: { [weak self] event in Task { @MainActor in
            guard let self else { return }; self.presentation.consume(event)
            if event.eventType == .jobCompleted { self.result = event.details.object ?? [:]; self.presentation.state = .completed; self.loadProjectHistory() }
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
        case .pipelinePlanned: interrupted
        case .processing, .pipelineRunning, .translating: processing
        case .interruptedResumable, .pipelinePaused: interrupted
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
        if !model.projects.isEmpty || !model.historyWarning.isEmpty {
            VStack(alignment: .leading, spacing: 8) {
                Text("Recent Projects").font(.headline)
                ForEach(model.projects.prefix(8)) { project in
                    HStack(spacing: 10) {
                        Image(systemName: project.availability == "AVAILABLE" ? "book.closed" : "exclamationmark.triangle").foregroundStyle(project.availability == "AVAILABLE" ? Color.accentColor : Color.orange)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(project.name).lineLimit(1)
                            Text(projectMetadata(project)).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                        }
                        Spacer()
                        Button(project.action) { model.openProject(project) }.disabled(!project.reopenAvailable)
                        Menu { Button("Remove from History", role: .destructive) { model.removeProject(project) } } label: { Image(systemName: "ellipsis.circle") }.menuStyle(.borderlessButton)
                    }.padding(.vertical, 6)
                }
                if !model.historyWarning.isEmpty { Text(model.historyWarning).font(.caption).foregroundStyle(.orange) }
            }.padding(.horizontal, 4)
        }
    }
    private var selectedDocument: some View {
        VStack(alignment: .leading, spacing: 18) {
            documentCard(status: model.ready ? "Ready" : "Checking requirements…", symbol: model.ready ? "checkmark.circle.fill" : "hourglass")
            workflowConfiguration
            if model.presentation.state == .preflighting { ProgressView().controlSize(.small) }
            HStack { Button("Start", action: model.start).buttonStyle(.borderedProminent).controlSize(.large).disabled(!model.ready); Button("Choose Another Document", action: chooseFiles); SettingsLink { Text("Options…") } }
        }.frame(maxWidth: 680, alignment: .leading)
    }
    private var workflowConfiguration: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Workflow").font(.headline)
            Label("1  Process document", systemImage: "1.circle")
            HStack { Label("2  Review", systemImage: "2.circle"); Picker("Review", selection: $model.workflowReviewPolicy) { Text("Skip review").tag("skip_human_review"); Text("Review when required").tag("review_when_required") }.labelsHidden().frame(width: 190) }
            Toggle("3  Translate English → Russian", isOn: $model.workflowTranslate)
            if model.workflowTranslate { HStack { Picker("Source", selection: $model.workflowTranslationSource) { Text("English → Russian").tag("en"); Text("Russian → English").tag("ru") }.labelsHidden(); TextField("Local model", text: $model.workflowTranslationModel).textFieldStyle(.roundedBorder).frame(width: 170) } }
            HStack { Label("4  Export", systemImage: "4.circle"); ForEach([("docx", "DOCX"), ("pdf", "PDF"), ("epub", "EPUB"), ("html", "HTML"), ("markdown", "Markdown")], id: \.0) { format in Toggle(format.1, isOn: workflowFormatBinding(format.0)).toggleStyle(.checkbox) } }
            Text("BookText validates and saves this sequential plan before work starts. Completed stages resume safely after an interruption.").font(.caption).foregroundStyle(.secondary)
        }.padding(14).background(Color.secondary.opacity(0.06)).clipShape(RoundedRectangle(cornerRadius: 10))
    }
    private func workflowFormatBinding(_ format: String) -> Binding<Bool> {
        Binding(get: { model.workflowExportFormats.contains(format) }, set: { selected in if selected { model.workflowExportFormats.insert(format) } else { model.workflowExportFormats.remove(format) } })
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
        VStack(alignment: .leading, spacing: 16) { documentCard(status: model.snapshot.state == .pipelinePlanned ? "Workflow ready" : "Workflow paused", symbol: model.snapshot.state == .pipelinePlanned ? "checkmark.circle" : "pause.circle"); Text("Completed work is saved. Resume continues from the current stage without repeating completed stages or chunks.").foregroundStyle(.secondary); if model.snapshot.state == .pipelinePaused || model.snapshot.state == .pipelinePlanned { Button(model.snapshot.state == .pipelinePlanned ? "Start Workflow" : "Resume Workflow") { model.runPipeline(resume: model.snapshot.state == .pipelinePaused) }.buttonStyle(.borderedProminent) } else { Button("Choose Source to Resume…", action: chooseFiles).buttonStyle(.borderedProminent) }; Button("Back to Documents", action: model.resetToChooser) }
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
        case .resumePipeline: Button("Resume Workflow") { model.runPipeline(resume: true) }.buttonStyle(.borderedProminent).controlSize(.large)
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
    private var lifecycleTitle: String { switch model.snapshot.state { case .reviewRequired: "Review required"; case .readyForFinalBuild: "Ready for final book"; case .translationReady: "Book and translation ready"; case .pipelinePaused: "Workflow paused"; default: "Book ready" } }
    private var lifecycleSymbol: String { model.snapshot.state == .reviewRequired ? "exclamationmark.circle.fill" : "checkmark.circle.fill" }
    private var progressDescription: String { if let current = model.presentation.current, let total = model.presentation.total { return "\(current) of \(total)" + (model.presentation.page.map { " · page \($0)" } ?? "") }; return model.presentation.page.map { "Page \($0)" } ?? "Working…" }
    private func refreshCurrentOutput() { if let output = model.outputURL { model.inspect(output) } }
    private func projectMetadata(_ project: ProjectHistoryEntry) -> String {
        let pages = project.pageCount.map { "\($0) pages" } ?? "pages unknown"
        let translation = project.translationState == "NOT_STARTED" ? "" : " · translation \(project.translationState.lowercased())"
        return "\(project.state.replacingOccurrences(of: "_", with: " ")) · \(pages)\(translation)"
    }
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
            Section("Processing") { Toggle("Create searchable PDF", isOn: $model.includePDF); Text("BookText automatically extracts usable PDF text and recognizes scanned text where needed. Images are preserved. Uncertain regions are flagged for review.").font(.caption).foregroundStyle(.secondary) }
            Section("Advanced and Diagnostics") {
                DisclosureGroup("BookText Core", isExpanded: $showDiagnostics) { TextField("Path to booktext.py", text: $model.corePath).textFieldStyle(.roundedBorder); HStack { Button("Check Requirements", action: model.preflight); Text("Machine protocol \(machineProtocolVersion)").foregroundStyle(.secondary) }; if !model.diagnostics.isEmpty { ScrollView { Text(model.diagnostics).font(.caption.monospaced()).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading) }.frame(maxHeight: 100) } }
                Button(model.cleanupBusy ? "Cleaning…" : "Clean Temporary Files", action: model.cleanTemporaryFiles).disabled(model.outputURL == nil || model.cleanupBusy)
                Text("Cleanup policy is validated and executed by BookText Core for the current output.").font(.caption).foregroundStyle(.secondary)
            }
        }.formStyle(.grouped).padding()
    }
}
