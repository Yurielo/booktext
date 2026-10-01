import Foundation

/// Presentation only. Core supplies classifications, text and warnings.
public enum ReaderBlockPresentation {
    public static func excerptNotice(_ block: [String: JSONValue]) -> String? {
        guard block["type"]?.string == "page_break",
              block["boundary_kind"]?.string == "excerpt_gap" else { return nil }
        return block["text"]?.string
    }

    public static func warnings(_ block: [String: JSONValue]) -> [String] {
        (block["normalization_warnings"]?.values ?? []).compactMap { $0.object?["message"]?.string }
    }
}
