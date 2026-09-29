import SwiftUI
import AppKit
import BookTextCore

@MainActor final class FinalBookModel: ObservableObject {
    @Published var inspection: [String: JSONValue] = [:]
    @Published var preview: [String: JSONValue] = [:]
    @Published var message = ""
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
    func export(_ format: String) { call(["export", outputURL.path, "--format", format]) { [weak self] detail in self?.exportCompleted(format, detail) } }
    private func exportCompleted(_ format: String, _ detail: [String: JSONValue]) {
        let generated = detail["result"]?.object?["generated"]?.object?[format]?.object
        let relative = generated?["named_deliverable"]?.string ?? generated?["path"]?.string
        if let relative, let url = safeFile(relative) { NSWorkspace.shared.open(url); message = "\(format.uppercased()) validated by Core and opened." }
        else { message = "\(format.uppercased()) validated by Core. Use Reveal in Finder to open it." }
        refresh()
    }
    func openArtifact(_ key: String) { guard let path = inspection["artifacts"]?.object?[key]?.object?["path"]?.string, let url = safeFile(path) else { return }; NSWorkspace.shared.open(url) }
    private func safeFile(_ relative: String) -> URL? {
        guard !relative.isEmpty, !relative.contains("..") else { return nil }
        let root = outputURL.standardizedFileURL; let candidate = root.appendingPathComponent(relative).standardizedFileURL
        return candidate.path.hasPrefix(root.path + "/") && FileManager.default.fileExists(atPath: candidate.path) ? candidate : nil
    }
}

struct FinalBookView: View {
    @StateObject private var model: FinalBookModel
    @Environment(\.dismiss) private var dismiss
    init(outputURL: URL, configuration: CoreConfiguration) { _model = StateObject(wrappedValue: FinalBookModel(outputURL: outputURL, configuration: configuration)) }
    var body: some View { VStack(alignment: .leading, spacing: 12) {
        HStack { Text("Final Book & Export").font(.title2.bold()); Spacer(); Button("Close") { dismiss() } }
        Text("Core remains the source of truth. These operations use existing artifacts only: no OCR, Vision, Tesseract, or LLM runs.").font(.caption).foregroundStyle(.secondary)
        summary
        HStack { Button("Build Final Book", action: model.buildFinal).buttonStyle(.borderedProminent); Button("Build / Refresh CDM", action: model.buildCDM); Divider().frame(height: 20); ForEach(["markdown", "html", "docx", "epub", "pdf"], id: \.self) { format in Button("Export \(format.uppercased())") { model.export(format) } } }.disabled(model.loading)
        HStack { Button("Open final text") { model.openArtifact("final_text") }.disabled(model.loading); Button("Open final Markdown") { model.openArtifact("final_markdown") }.disabled(model.loading); Button("Reveal exports") { model.openArtifact("exports") }.disabled(model.loading) }
        if model.loading { ProgressView() }
        if !model.message.isEmpty { Text(model.message).font(.caption).foregroundStyle(.secondary) }
        Divider(); preview
    }.padding(20).frame(minWidth: 900, minHeight: 650).onAppear { model.refresh() } }

    @ViewBuilder private var summary: some View {
        let review = model.inspection["review"]?.object
        let counts = model.preview["block_counts"]?.object
        Text("Unresolved HIGH/MEDIUM: \(review?["unresolved_priority_counts"]?.object?["HIGH"]?.integer ?? 0) / \(review?["unresolved_priority_counts"]?.object?["MEDIUM"]?.integer ?? 0) · stale/conflicts: \(review?["stale_or_conflict_count"]?.integer ?? 0)")
        Text("CDM blocks: \(model.preview["count"]?.integer ?? 0) · tables: \(counts?["table"]?.integer ?? 0) · figures: \(counts?["figure"]?.integer ?? 0) · unknown: \(counts?["unknown"]?.integer ?? 0)").font(.caption).foregroundStyle(.secondary)
        if let warnings = model.preview["warnings"]?.values, !warnings.isEmpty { Text("Warnings: \(warnings.count) (fallback/unknown content remains explicit in CDM and exports)").font(.caption).foregroundStyle(.orange) }
    }

    @ViewBuilder private var preview: some View {
        let blocks = model.preview["blocks"]?.values ?? []
        let index = model.preview["index"]?.integer ?? 0
        let total = model.preview["count"]?.integer ?? 0
        HStack { Text("Canonical structure").font(.headline); Spacer(); Button("Previous") { model.loadPreview(index: max(0, index - 20)) }.disabled(index == 0 || model.loading); Button("Next") { model.loadPreview(index: index + 20) }.disabled(index + blocks.count >= total || model.loading); Text("\(min(index + 1, total))–\(min(index + blocks.count, total)) of \(total)").font(.caption) }
        ScrollView { LazyVStack(alignment: .leading, spacing: 8) { ForEach(Array(blocks.enumerated()), id: \.offset) { _, value in
            if let block = value.object { VStack(alignment: .leading, spacing: 3) { Text("\(block["type"]?.string ?? "unknown") · source page \(block["provenance"]?.object?["source_page"]?.integer ?? 0)").font(.caption.bold()); Text(block["text"]?.string ?? (block["table_status"]?.string == "fallback_review" ? "Unverified fallback table — no cells are displayed." : "Figure / non-text block")).lineLimit(4); if block["table_status"]?.string == "fallback_review" { Text("Fallback evidence, not verified table data.").font(.caption).foregroundStyle(.orange) } }.padding(.vertical, 4).frame(maxWidth: .infinity, alignment: .leading) }
        } } }
    }
}
