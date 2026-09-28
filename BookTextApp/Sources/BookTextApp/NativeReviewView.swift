import SwiftUI
import AppKit
import BookTextCore

// This is deliberately a thin native client.  Queue ordering, statuses,
// decision validation, batches, and persistence are all supplied by Core.
@MainActor final class NativeReviewModel: ObservableObject {
    enum Mode: String, CaseIterable { case items = "Items", clusters = "Clusters" }
    @Published var mode: Mode = .items { didSet { index = 0; load() } }
    @Published var priority = "" { didSet { index = 0; load() } }
    @Published var status = "" { didSet { index = 0; load() } }
    @Published var pageFilter = "" { didSet { index = 0; load() } }
    @Published var index = 0
    @Published var total = 0
    @Published var item: [String: JSONValue] = [:]
    @Published var cluster: [String: JSONValue] = [:]
    @Published var progress: [String: JSONValue] = [:]
    @Published var message = ""
    @Published var loading = false
    @Published var manualSheet = false
    @Published var manualText = ""
    @Published var batchPreview: [String: JSONValue] = [:]
    @Published var excludedMembers: Set<String> = []
    @Published var batchAction = "keep_surya"
    @Published var batchIDToRevert = ""
    private let outputURL: URL
    private let configuration: CoreConfiguration
    private var bridge: CoreProcessBridge?

    init(outputURL: URL, configuration: CoreConfiguration) { self.outputURL = outputURL; self.configuration = configuration }
    private func filters() -> [String] {
        var values: [String] = []
        if !priority.isEmpty { values += ["--priority", priority] }
        if !status.isEmpty { values += ["--status", status] }
        if let page = Int(pageFilter), page > 0 { values += ["--page", String(page)] }
        return values
    }
    private func call(_ arguments: [String], completion: @escaping @MainActor @Sendable ([String: JSONValue]) -> Void) {
        loading = true; message = ""
        let client = CoreProcessBridge(configuration: configuration); bridge = client
        do {
            try client.start(arguments: arguments, event: { [weak self] event in
                Task { @MainActor in
                    guard let self else { return }
                    if event.eventType == .jobCompleted { completion(event.details.object ?? [:]) }
                    if event.eventType == .jobFailed { self.message = "\(event.code): \(event.message)" }
                }
            }, stderr: { _ in }, finished: { [weak self] _ in Task { @MainActor in self?.loading = false } })
        } catch { loading = false; message = error.localizedDescription }
    }
    func load() {
        let command = mode == .items ? "review-item" : "review-clusters"
        let args = [command, outputURL.path] + filters() + ["--index", String(index)]
        call(args) { [weak self] details in
            guard let self else { return }
            self.total = details["count"]?.integer ?? 0
            self.index = details["index"]?.integer ?? 0
            self.progress = details["progress"]?.object ?? [:]
            if self.mode == .items { self.item = details["item"]?.object ?? [:]; self.cluster = [:] }
            else { self.cluster = details["cluster"]?.object ?? [:]; self.item = [:]; self.excludedMembers = [] }
        }
    }
    func previous() { guard index > 0 else { return }; index -= 1; load() }
    func next() { guard index + 1 < total else { return }; index += 1; load() }
    func nextUnresolved() { mode = .items; status = "unresolved"; index = 0; load() }
    func decide(_ action: String, replacement: String? = nil) {
        guard let id = item["item_id"]?.string else { return }
        var args = ["decision", outputURL.path, "--item", id, "--action", action, "--replace"]
        if let replacement { args += ["--replacement", replacement] }
        call(args) { [weak self] _ in self?.message = "Decision saved atomically by Core."; self?.load() }
    }
    func previewBatch() {
        guard let id = cluster["cluster_id"]?.string else { return }
        let args = ["batch-preview", outputURL.path, "--cluster", id, "--action", batchAction] + excludedMembers.flatMap { ["--exclude", $0] }
        call(args) { [weak self] details in self?.batchPreview = details["result"]?.object ?? [:] }
    }
    func confirmBatch() {
        guard let id = cluster["cluster_id"]?.string else { return }
        let args = ["batch-confirm", outputURL.path, "--cluster", id, "--action", batchAction, "--confirmed"] + excludedMembers.flatMap { ["--exclude", $0] }
        call(args) { [weak self] details in self?.message = "Batch result: \(details["result"]?.object?["applied"]?.bool == true ? "saved atomically" : "not applied")"; self?.batchPreview = [:]; self?.load() }
    }
    func revertBatch(_ batchID: String) {
        call(["batch-revert", outputURL.path, "--batch-id", batchID]) { [weak self] _ in self?.message = "Batch reverted by Core."; self?.load() }
    }
    func buildFinal() {
        call(["final-build", outputURL.path]) { [weak self] details in
            let report = details["result"]?.object ?? [:]
            self?.message = "Final build complete. Applied: \(report["applied_text_changes"]?.integer ?? 0); unresolved HIGH/MEDIUM remain visible in Core report."
        }
    }
    func localPageImage() -> NSImage? {
        guard let path = item["source_asset"]?.object?["page_image"]?.string, !path.contains("..") else { return nil }
        let url = outputURL.appendingPathComponent(path).standardizedFileURL
        guard url.path.hasPrefix(outputURL.standardizedFileURL.path + "/") else { return nil }
        return NSImage(contentsOf: url)
    }
}

struct NativeReviewView: View {
    @StateObject private var model: NativeReviewModel
    @Environment(\.dismiss) private var dismiss
    init(outputURL: URL, configuration: CoreConfiguration) { _model = StateObject(wrappedValue: NativeReviewModel(outputURL: outputURL, configuration: configuration)) }
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack { Text("Native Review").font(.title2.bold()); Spacer(); Button("Close") { dismiss() } }
            Text("Surya is primary OCR. Vision is independent evidence; all decisions and validation remain in BookText Core.").font(.caption).foregroundStyle(.secondary)
            filters
            progress
            HStack { Button("Previous", action: model.previous).disabled(model.index == 0 || model.loading); Button("Next", action: model.next).disabled(model.index + 1 >= model.total || model.loading); Button("Next unresolved", action: model.nextUnresolved); Spacer(); Button("Build Final Book", action: model.buildFinal).buttonStyle(.borderedProminent) }
            if model.loading { ProgressView() }
            if !model.message.isEmpty { Text(model.message).font(.caption).foregroundStyle(.orange).textSelection(.enabled) }
            Divider(); if model.mode == .items { itemView } else { clusterView }
        }.padding(20).frame(minWidth: 900, minHeight: 700).onAppear { model.load() }
        .sheet(isPresented: $model.manualSheet) { manualSheet }
    }
    private var filters: some View { HStack {
        Picker("View", selection: $model.mode) { ForEach(NativeReviewModel.Mode.allCases, id: \.self) { Text($0.rawValue).tag($0) } }.frame(width: 130)
        Picker("Priority", selection: $model.priority) { Text("All priorities").tag(""); ForEach(["HIGH", "MEDIUM", "LOW", "INFO"], id: \.self) { Text($0).tag($0) } }.frame(width: 150)
        Picker("Status", selection: $model.status) { Text("All statuses").tag(""); ForEach(["unresolved", "resolved_changed", "resolved_no_change", "deferred", "stale", "conflict"], id: \.self) { Text($0).tag($0) } }.frame(width: 180)
        TextField("Page", text: $model.pageFilter).frame(width: 80); Button("Apply") { model.index = 0; model.load() }
    } }
    private var progress: some View { let p = model.progress; return Text("Queue \(model.index + 1)/\(model.total) · total \(p["total"]?.integer ?? 0) · unresolved \(p["unresolved"]?.integer ?? 0) · changed \(p["resolved_changed"]?.integer ?? 0) · no-change \(p["resolved_no_change"]?.integer ?? 0) · deferred \(p["deferred"]?.integer ?? 0) · stale/conflict \(p["stale_conflict"]?.integer ?? 0) · HIGH \(p["unresolved_high"]?.integer ?? 0) · MEDIUM \(p["unresolved_medium"]?.integer ?? 0)").font(.caption).textSelection(.enabled) }
    @ViewBuilder private var itemView: some View {
        if model.item.isEmpty { ContentUnavailableView("No matching review items", systemImage: "checkmark.circle") }
        else { HSplitView {
            ScrollView { if let image = model.localPageImage() { Image(nsImage: image).resizable().scaledToFit().accessibilityLabel("Source page \(model.item["page"]?.integer ?? 0)") } else { ContentUnavailableView("Source page unavailable", systemImage: "doc") } }.frame(minWidth: 360)
            ScrollView { VStack(alignment: .leading, spacing: 10) {
                Text("\(model.item["priority"]?.string ?? "") · page \(model.item["page"]?.integer ?? 0)").font(.title3.bold())
                detail("Item ID", model.item["item_id"]?.string ?? ""); detail("Status", model.item["status"]?.object?["status"]?.string ?? "unresolved"); detail("Reasons", model.item["reasons"]?.values?.compactMap { $0.string }.joined(separator: ", ") ?? "—"); detail("Signals / QA", String(describing: model.item["signals"] ?? .null)); detail("Surya", model.item["surya"]?.string ?? "—"); detail("Vision alternative", model.item["vision"]?.string ?? "—")
                HStack { Button("Keep Surya") { model.decide("keep_surya") }; Button("Accept Vision") { model.decide("accept_vision") }.disabled((model.item["vision"]?.string ?? "").isEmpty); Button("Manual") { model.manualText = model.item["surya"]?.string ?? ""; model.manualSheet = true }; Button("Ignore") { model.decide("ignore") }; Button("Defer") { model.decide("defer") } }
            }.padding(8) }.frame(minWidth: 430)
        } }
    }
    @ViewBuilder private var clusterView: some View {
        if model.cluster.isEmpty { ContentUnavailableView("No matching clusters", systemImage: "circle.grid.2x2") }
        else { ScrollView { VStack(alignment: .leading, spacing: 10) {
            Text("\(model.cluster["category"]?.string ?? "cluster") · \(model.cluster["member_count"]?.integer ?? 0) members").font(.title3.bold())
            Text("Batch eligible: \(model.cluster["batch_eligible"]?.bool == true ? "yes" : "no") · members remain individually unresolved until individually decided.").font(.caption)
            Picker("Batch action", selection: $model.batchAction) { ForEach(model.cluster["allowed_batch_actions"]?.values?.compactMap { $0.string } ?? ["keep_surya"], id: \.self) { Text($0).tag($0) } }.frame(width: 240)
            ForEach(Array((model.cluster["members"]?.values ?? []).enumerated()), id: \.offset) { _, value in if let member = value.object, let id = member["item_id"]?.string { Toggle(isOn: Binding(get: { !model.excludedMembers.contains(id) }, set: { includeMember in if includeMember { model.excludedMembers.remove(id) } else { model.excludedMembers.insert(id) } })) { Text("\(id) · page \(member["page"]?.integer ?? 0) · \(member["priority"]?.string ?? "") · \(member["status"]?.string ?? "")") }.disabled(!(model.cluster["eligible_member_ids"]?.values?.compactMap { $0.string }.contains(id) ?? false)) } }
            if model.cluster["batch_eligible"]?.bool == true { Button("Preview exact batch") { model.previewBatch() }.buttonStyle(.borderedProminent) }
            if !model.batchPreview.isEmpty { GroupBox("Batch preview — no decision has been saved") { VStack(alignment: .leading) { Text("Action: \(model.batchPreview["action"]?.string ?? "") · members: \(model.batchPreview["member_ids"]?.values?.count ?? 0)"); ForEach(Array((model.batchPreview["members"]?.values ?? []).enumerated()), id: \.offset) { _, value in if let member = value.object { Text("\(member["item_id"]?.string ?? "") · p\(member["page"]?.integer ?? 0) · \(member["priority"]?.string ?? "") · \(member["reasons"]?.values?.compactMap { $0.string }.joined(separator: ", ") ?? "")").font(.caption) } }; Text("Core will revalidate every anchor before atomic persistence.").font(.caption); Button("Final confirmation: apply this exact batch") { model.confirmBatch() }.buttonStyle(.borderedProminent) } } }
            HStack { TextField("Confirmed batch ID to revert", text: $model.batchIDToRevert); Button("Revert batch") { model.revertBatch(model.batchIDToRevert) }.disabled(model.batchIDToRevert.isEmpty) }
        }.padding(8) } }
    }
    private var manualSheet: some View { VStack(alignment: .leading, spacing: 12) { Text("Manual correction").font(.headline); Text("Surya: \(model.item["surya"]?.string ?? "—")"); Text("Vision: \(model.item["vision"]?.string ?? "—")"); TextEditor(text: $model.manualText).frame(minHeight: 160).autocorrectionDisabled(); HStack { Spacer(); Button("Cancel") { model.manualSheet = false }; Button("Save manual decision") { model.decide("manual", replacement: model.manualText); model.manualSheet = false }.buttonStyle(.borderedProminent) } }.padding().frame(width: 600) }
    private func detail(_ title: String, _ value: String) -> some View { VStack(alignment: .leading, spacing: 2) { Text(title).font(.caption.bold()); Text(value).textSelection(.enabled) } }
}
