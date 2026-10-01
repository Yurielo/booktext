/// Presentation only. All source analysis and engine routing live in Core.
public enum AutomaticProcessingOptions {
    public static func arguments(includeSearchablePDF: Bool) -> [String] {
        ["--ocr", "auto", "--no-descriptions"] + (includeSearchablePDF ? ["--searchable-pdf"] : ["--no-pdf"])
    }
}
