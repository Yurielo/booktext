import Foundation

/// A document-centric projection of authoritative Core/job data.  This type is
/// intentionally presentation-only: it neither persists workflow state nor
/// decides whether Core operations are valid.
public enum DocumentLifecycleState: String, Equatable, Sendable {
    case noDocument
    case readyToProcess
    case processing
    case interruptedResumable
    case reviewRequired
    case readyForFinalBuild
    case bookReady
    case translating
    case translationReady
    case failed
}

public enum DocumentPrimaryAction: String, Equatable, Sendable {
    case chooseDocument
    case startProcessing
    case cancelProcessing
    case resumeProcessing
    case reviewItems
    case createFinalBook
    case openBook
    case cancelTranslation
    case openTranslation
    case tryAgain
}

public struct DocumentPresentationSnapshot: Equatable, Sendable {
    public let state: DocumentLifecycleState
    public let primaryAction: DocumentPrimaryAction
    public let pageCount: Int?
    public let unresolvedHigh: Int
    public let unresolvedMedium: Int
    public let finalBookAvailable: Bool
    public let cdmAvailable: Bool
    public let translationAvailable: Bool

    public init(
        state: DocumentLifecycleState,
        primaryAction: DocumentPrimaryAction,
        pageCount: Int? = nil,
        unresolvedHigh: Int = 0,
        unresolvedMedium: Int = 0,
        finalBookAvailable: Bool = false,
        cdmAvailable: Bool = false,
        translationAvailable: Bool = false
    ) {
        self.state = state
        self.primaryAction = primaryAction
        self.pageCount = pageCount
        self.unresolvedHigh = unresolvedHigh
        self.unresolvedMedium = unresolvedMedium
        self.finalBookAvailable = finalBookAvailable
        self.cdmAvailable = cdmAvailable
        self.translationAvailable = translationAvailable
    }
}

public enum DocumentPresentationMapper {
    public static func snapshot(
        sourceSelected: Bool,
        outputSelected: Bool,
        jobState: JobPresentationState,
        inspection: [String: JSONValue],
        isTranslating: Bool = false,
        translationResumable: Bool = false
    ) -> DocumentPresentationSnapshot {
        let artifacts = inspection["artifacts"]?.object ?? [:]
        let review = inspection["review"]?.object ?? [:]
        let unresolved = review["unresolved_priority_counts"]?.object ?? [:]
        let high = unresolved["HIGH"]?.integer ?? 0
        let medium = unresolved["MEDIUM"]?.integer ?? 0
        let pages = inspection["run_summary"]?.object?["processed"]?.integer
        let finalBook = artifactExists("final_build", in: artifacts)
        let cdm = inspection["cdm"]?.object?["present"]?.bool == true || artifactExists("cdm", in: artifacts)
        let translation = artifactExists("translations", in: artifacts)

        let state: DocumentLifecycleState
        if jobState == .failed {
            state = .failed
        } else if isTranslating {
            state = .translating
        } else if jobState == .running || jobState == .cancelling {
            state = .processing
        } else if jobState == .cancelled || translationResumable {
            state = .interruptedResumable
        } else if outputSelected {
            if high + medium > 0 {
                state = .reviewRequired
            } else if !finalBook || !cdm {
                state = .readyForFinalBuild
            } else if translation {
                state = .translationReady
            } else {
                state = .bookReady
            }
        } else if sourceSelected {
            state = .readyToProcess
        } else {
            state = .noDocument
        }

        return DocumentPresentationSnapshot(
            state: state,
            primaryAction: primaryAction(for: state),
            pageCount: pages,
            unresolvedHigh: high,
            unresolvedMedium: medium,
            finalBookAvailable: finalBook,
            cdmAvailable: cdm,
            translationAvailable: translation
        )
    }

    public static func primaryAction(for state: DocumentLifecycleState) -> DocumentPrimaryAction {
        switch state {
        case .noDocument: .chooseDocument
        case .readyToProcess: .startProcessing
        case .processing: .cancelProcessing
        case .interruptedResumable: .resumeProcessing
        case .reviewRequired: .reviewItems
        case .readyForFinalBuild: .createFinalBook
        case .bookReady: .openBook
        case .translating: .cancelTranslation
        case .translationReady: .openTranslation
        case .failed: .tryAgain
        }
    }

    public static func phaseLabel(_ phase: String?) -> String {
        switch phase {
        case "preflight": "Checking requirements"
        case "prepare": "Preparing document"
        case "ocr": "Recognizing text"
        case "qa": "Checking recognition"
        case "visuals", "tables": "Processing images and tables"
        case "review": "Preparing review"
        case "final_build": "Creating final book"
        case "cdm": "Preparing book structure"
        case "export": "Exporting book"
        case "translation": "Translating book"
        default: "Processing document"
        }
    }

    private static func artifactExists(_ key: String, in artifacts: [String: JSONValue]) -> Bool {
        artifacts[key]?.object?["exists"]?.bool == true
    }
}

/// Normalizes the small persisted recent-output list.  It stores only paths;
/// lifecycle status is always refreshed from Core when the user opens one.
public enum RecentOutputList {
    public static func normalized(_ paths: [String], adding path: String? = nil, limit: Int = 5) -> [String] {
        var candidates: [String] = []
        if let path, !path.isEmpty { candidates.append(path) }
        candidates.append(contentsOf: paths)
        var seen = Set<String>()
        return candidates.compactMap { raw in
            guard !raw.isEmpty, !raw.contains("\0") else { return nil }
            let normalized = URL(fileURLWithPath: raw).standardizedFileURL.path
            guard seen.insert(normalized).inserted else { return nil }
            return normalized
        }.prefix(max(0, limit)).map { $0 }
    }
}

public enum ExportFormatSelection {
    public static let supported = ["markdown", "html", "docx", "epub", "pdf", "pdf_image"]
    public static func ordered(_ selected: Set<String>) -> [String] {
        supported.filter(selected.contains)
    }
}
