import SwiftUI
import AppKit
import BookTextCore

@MainActor final class FinalBookModel: ObservableObject {
    @Published var inspection: [String: JSONValue] = [:]
    @Published var preview: [String: JSONValue] = [:]
    @Published var message = ""
    @Published var completedExportPath: String?
    @Published var loading = false
    private let outputURL: URL
    private let configuration: CoreConfiguration
    private var bridge: CoreProcessBridge?
    private let pageSize = 20

    init(outputURL: URL, configuration: CoreConfiguration) { self.outputURL = outputURL; self.configuration = configuration }
    private func call(_ arguments: [String], completed: @escaping @MainActor ([String: JSONValue]) -> Void) {
        loading = true; message = ""
        let client = CoreProcessBridge(configuration: configuration); bridge = client
        do { try client.start(arguments: arguments, event: { [weak self] event in Task { @MainActor in
            guard let self else { return }
            if event.eventType == .jobCompleted { completed(event.details.object ?? [:]) }
            if event.eventType == .jobFailed { self.message = "\(event.code): \(event.message)" }
        } }, stderr: { _ in }, finished: { [weak self] _ in Task { @MainActor in self?.loading = false } }) }
        catch { loading = false; message = error.localizedDescription }
    }
    func refresh() {
        call(["inspect", outputURL.path]) { [weak self] detail in
            self?.inspection = detail
            if detail["cdm"]?.object?["present"]?.bool == true { self?.loadPreview(index: self?.preview["index"]?.integer ?? 0) }
        }
    }
    func loadPreview(index: Int) {
        call(["document-preview", outputURL.path, "--index", String(index), "--limit", String(pageSize)]) { [weak self] detail in self?.preview = detail["result"]?.object ?? [:] }
    }
    func buildFinal() { call(["final-build", outputURL.path]) { [weak self] detail in self?.message = "Final book built. Applied changes: \(detail["result"]?.object?["applied_text_changes"]?.integer ?? 0)."; self?.refresh() } }
    func buildCDM() { call(["cdm", outputURL.path]) { [weak self] _ in self?.message = "CDM rebuilt from existing artifacts; no OCR or model ran."; self?.loadPreview(index: 0); self?.refresh() } }
    func export(_ formats: [String]) {
        guard !formats.isEmpty else { message = "Select at least one export format."; return }
        call(["export", outputURL.path] + formats.flatMap { ["--format", $0] }) { [weak self] detail in self?.exportCompleted(formats, detail) }
    }
    func translate(sourceLanguage: String, targetLanguage: String, model: String) {
        guard !model.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { message = "Choose a local Ollama model before translating."; return }
        call(["translation", outputURL.path, "--source-language", sourceLanguage, "--target-language", targetLanguage, "--provider", "ollama", "--model", model]) { [weak self] detail in
            let result = detail["result"]?.object ?? [:]
            let reused = result["reused"]?.bool == true
            self?.message = reused ? "Existing source-matched translation is available." : "Translation created from CDM; it is machine-translated, not human-reviewed."
            self?.refresh()
        }
    }
    func exportTranslation(sourceLanguage: String, targetLanguage: String, formats: [String]) {
        guard !formats.isEmpty else { message = "Select at least one translation export format."; return }
        call(["translation-export", outputURL.path, "--source-language", sourceLanguage, "--target-language", targetLanguage] + formats.flatMap { ["--format", $0] }) { [weak self] detail in
            self?.exportCompleted(formats, detail, label: "Translated")
        }
    }
    private func exportCompleted(_ formats: [String], _ detail: [String: JSONValue], label: String = "") {
        completedExportPath = nil
        guard formats.count == 1, let format = formats.first else {
            message = "\(label.isEmpty ? "Selected" : label) formats were validated by Core. Reveal exports to open them."
            refresh()
            return
        }
        let generated = detail["result"]?.object?["generated"]?.object?[format]?.object
        let relative = generated?["named_deliverable"]?.string ?? generated?["path"]?.string
        if let relative, safeFile(relative) != nil { completedExportPath = relative; message = "\(label.isEmpty ? "" : label + " ")\(format.uppercased()) validated by Core. Open or Reveal it when you are ready." }
        else { message = "\(format.uppercased()) validated by Core. Use Reveal exports to locate it." }
        refresh()
    }
    func openCompletedExport() { guard let relative = completedExportPath, let url = safeFile(relative) else { return }; NSWorkspace.shared.open(url) }
    func revealCompletedExport() { guard let relative = completedExportPath, let url = safeFile(relative) else { return }; NSWorkspace.shared.activateFileViewerSelecting([url]) }
    func openArtifact(_ key: String) { guard let path = inspection["artifacts"]?.object?[key]?.object?["path"]?.string, let url = safeFile(path) else { return }; NSWorkspace.shared.open(url) }
    func localImage(_ block: [String: JSONValue]) -> NSImage? {
        guard let path = block["image"]?.string, let url = safeFile(path) else { return nil }
        return NSImage(contentsOf: url)
    }
    private func safeFile(_ relative: String) -> URL? {
        guard !relative.isEmpty, !relative.contains("..") else { return nil }
        let root = outputURL.standardizedFileURL; let candidate = root.appendingPathComponent(relative).standardizedFileURL
        return candidate.path.hasPrefix(root.path + "/") && FileManager.default.fileExists(atPath: candidate.path) ? candidate : nil
    }
}

struct FinalBookView: View {
    @StateObject private var model: FinalBookModel
    @State private var showStructure = false
    @State private var translationSource = "en"
    @State private var translationModel = "qwen3:8b"
    @State private var selectedFormats: Set<String> = ["docx"]
    @Environment(\.dismiss) private var dismiss
    init(outputURL: URL, configuration: CoreConfiguration) { _model = StateObject(wrappedValue: FinalBookModel(outputURL: outputURL, configuration: configuration)) }
    var body: some View { VStack(alignment: .leading, spacing: 12) {
        HStack { Text("Final Book & Export").font(.title2.bold()); Spacer(); Button("Close") { dismiss() } }
        Text("Core remains the source of truth. Final build, CDM, and export use existing artifacts only: no OCR, Vision, Tesseract, or LLM runs. Translation is an explicit optional local-model operation below.").font(.caption).foregroundStyle(.secondary)
        summary
        HStack {
            Button("Build Final Book", action: model.buildFinal).buttonStyle(.borderedProminent)
            Button("Build / Refresh CDM", action: model.buildCDM)
            Divider().frame(height: 20)
            Menu("Formats (\(selectedFormats.count))") {
                ForEach(Self.exportFormats, id: \.id) { format in
                    Toggle(format.label, isOn: formatSelection(format.id))
                }
                Divider()
                Button("Select all") { selectedFormats = Set(Self.exportFormats.map(\.id)) }
                Button("Clear selection") { selectedFormats.removeAll() }
            }
            Button("Export selected") { model.export(Self.exportFormats.map(\.id).filter(selectedFormats.contains)) }
                .disabled(selectedFormats.isEmpty)
            Button("Export all") { model.export(["all"]) }
        }.disabled(model.loading)
        HStack { Button("Open final text") { model.openArtifact("final_text") }.disabled(model.loading); Button("Open final Markdown") { model.openArtifact("final_markdown") }.disabled(model.loading); Button("Reveal exports") { model.openArtifact("exports") }.disabled(model.loading); Button("Reveal translations") { model.openArtifact("translations") }.disabled(model.loading) }
        translationControls
        if model.loading { ProgressView() }
        if !model.message.isEmpty { Text(model.message).font(.caption).foregroundStyle(.secondary) }
        if model.completedExportPath != nil { HStack { Button("Open exported file", action: model.openCompletedExport); Button("Reveal exported file", action: model.revealCompletedExport) }.disabled(model.loading) }
        Divider(); readerPreview
        DisclosureGroup("Technical structure / diagnostics", isExpanded: $showStructure) { structureInspector }
    }.padding(20).frame(minWidth: 900, minHeight: 650).onAppear { model.refresh() } }

    @ViewBuilder private var summary: some View {
        let review = model.inspection["review"]?.object
        let counts = model.preview["block_counts"]?.object
        Text("Unresolved HIGH/MEDIUM: \(review?["unresolved_priority_counts"]?.object?["HIGH"]?.integer ?? 0) / \(review?["unresolved_priority_counts"]?.object?["MEDIUM"]?.integer ?? 0) · stale/conflicts: \(review?["stale_or_conflict_count"]?.integer ?? 0)")
        Text("CDM blocks: \(model.preview["count"]?.integer ?? 0) · tables: \(counts?["table"]?.integer ?? 0) · figures: \(counts?["figure"]?.integer ?? 0) · unknown: \(counts?["unknown"]?.integer ?? 0)").font(.caption).foregroundStyle(.secondary)
        if let warnings = model.preview["warnings"]?.values, !warnings.isEmpty { Text("Warnings: \(warnings.count) (fallback/unknown content remains explicit in CDM and exports)").font(.caption).foregroundStyle(.orange) }
    }

    private static let exportFormats: [(id: String, label: String)] = [
        ("markdown", "Markdown"), ("html", "HTML"), ("docx", "DOCX"),
        ("epub", "EPUB"), ("pdf", "PDF (publication)"),
        ("pdf_image", "PDF (image-only; no selectable text)")
    ]

    private func formatSelection(_ format: String) -> Binding<Bool> {
        Binding(get: { selectedFormats.contains(format) }, set: { selected in
            if selected { selectedFormats.insert(format) } else { selectedFormats.remove(format) }
        })
    }

    @ViewBuilder private var translationControls: some View {
        HStack(spacing: 8) {
            Text("Derived translation").font(.caption.weight(.semibold))
            Picker("Source", selection: $translationSource) { Text("EN → RU").tag("en"); Text("RU → EN").tag("ru") }.labelsHidden().frame(width: 110)
            TextField("Local Ollama model", text: $translationModel).textFieldStyle(.roundedBorder).frame(width: 150)
            Button("Translate") { model.translate(sourceLanguage: translationSource, targetLanguage: translationSource == "en" ? "ru" : "en", model: translationModel) }
            Button("Export translation") { model.exportTranslation(sourceLanguage: translationSource, targetLanguage: translationSource == "en" ? "ru" : "en", formats: Self.exportFormats.map(\.id).filter(selectedFormats.contains)) }
        }.disabled(model.loading)
        Text("Translate is an optional local Ollama operation. Export translation uses only the existing validated translation and CDM — no OCR or model runs.").font(.caption2).foregroundStyle(.secondary)
    }

    @ViewBuilder private var readerPreview: some View {
        let blocks = model.preview["blocks"]?.values ?? []
        let index = model.preview["index"]?.integer ?? 0
        let total = model.preview["count"]?.integer ?? 0
        HStack { Text("Book preview").font(.headline); Spacer(); Button("Previous") { model.loadPreview(index: max(0, index - 20)) }.disabled(index == 0 || model.loading); Button("Next") { model.loadPreview(index: index + 20) }.disabled(index + blocks.count >= total || model.loading); Text("\(min(index + 1, total))–\(min(index + blocks.count, total)) of \(total)").font(.caption) }
        ScrollView { LazyVStack(alignment: .leading, spacing: 14) {
            if index == 0, let title = model.preview["metadata"]?.object?["title"]?.string, !title.isEmpty {
                VStack(spacing: 8) { Spacer(minLength: 28); Text(title).font(.title.bold()).multilineTextAlignment(.center); Spacer(minLength: 20) }.frame(maxWidth: .infinity)
            }
            ForEach(Array(blocks.enumerated()), id: \.offset) { _, value in if let block = value.object { readerBlock(block) } }
        }.frame(maxWidth: 760, alignment: .leading).padding(.horizontal, 32).padding(.vertical, 12) }
    }

    @ViewBuilder private func readerBlock(_ block: [String: JSONValue]) -> some View {
        let type = block["type"]?.string ?? "unknown"
        switch type {
        case "heading": Text(block["text"]?.string ?? "").font(.title3.weight(.semibold)).padding(.top, 12)
        case "paragraph", "unknown": Text(block["text"]?.string ?? "").font(.body).fixedSize(horizontal: false, vertical: true)
        case "list": Text(block["text"]?.string ?? "").font(.body).padding(.leading, 20).fixedSize(horizontal: false, vertical: true)
        case "page_break":
            VStack(spacing: 6) { Divider(); Text("Source page \(block["provenance"]?.object?["source_page"]?.integer ?? 0)").font(.caption).foregroundStyle(.secondary) }.padding(.vertical, 10)
        case "figure":
            VStack(alignment: .leading, spacing: 6) {
                if let image = model.localImage(block) { Image(nsImage: image).resizable().scaledToFit().frame(maxWidth: 680) }
                if let description = block["description"]?.string, !description.isEmpty { Text(description).font(.caption).foregroundStyle(.secondary) }
            }.frame(maxWidth: .infinity, alignment: .center)
        case "table": tableBlock(block)
        case "caption": Text(block["text"]?.string ?? "").font(.caption).foregroundStyle(.secondary).italic()
        default: Text(block["text"]?.string ?? "").font(.body)
        }
    }

    @ViewBuilder private func tableBlock(_ block: [String: JSONValue]) -> some View {
        if block["table_status"]?.string == "fallback_review" {
            Text("Table preserved as review evidence; no unverified cells are shown.").font(.caption).foregroundStyle(.orange).padding(8).frame(maxWidth: .infinity, alignment: .leading).background(Color.orange.opacity(0.08)).clipShape(RoundedRectangle(cornerRadius: 6))
        } else {
            let rows = block["table_preview"]?.values ?? []
            VStack(alignment: .leading, spacing: 3) {
                ForEach(Array(rows.enumerated()), id: \.offset) { rowIndex, value in
                    HStack(spacing: 0) { ForEach(Array((value.values ?? []).enumerated()), id: \.offset) { _, cell in Text(cell.string ?? "").font(.body).frame(minWidth: 80, maxWidth: .infinity, alignment: .leading).padding(6).overlay(Rectangle().stroke(Color.secondary.opacity(0.25), lineWidth: 0.5)) } }
                        .fontWeight(rowIndex == 0 ? .semibold : .regular)
                }
            }.frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    @ViewBuilder private var structureInspector: some View {
        let blocks = model.preview["blocks"]?.values ?? []
        ScrollView { LazyVStack(alignment: .leading, spacing: 5) { ForEach(Array(blocks.enumerated()), id: \.offset) { _, value in
            if let block = value.object { Text("\(block["type"]?.string ?? "unknown") · source page \(block["provenance"]?.object?["source_page"]?.integer ?? 0)").font(.caption.monospaced()); Text(block["text"]?.string ?? "").font(.caption).lineLimit(2).foregroundStyle(.secondary) }
        } }.padding(.top, 4) }.frame(maxHeight: 180)
    }
}
