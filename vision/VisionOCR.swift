import Foundation
import Vision

struct BoxJSON: Codable {
    let x: Double
    let y: Double
    let width: Double
    let height: Double
}

struct CandidateJSON: Codable {
    let text: String
    let confidence: Float
}

struct WordJSON: Codable {
    let text: String
    let confidence: Float?
    let bbox: BoxJSON
    let candidates: [CandidateJSON]
}

struct LineJSON: Codable {
    let text: String
    let confidence: Float?
    let bbox: BoxJSON
    let isTitle: Bool
}

struct TextBlockJSON: Codable {
    let text: String
    let bbox: BoxJSON
}

struct TableCellJSON: Codable {
    let text: String
    let rowStart: Int
    let rowEnd: Int
    let columnStart: Int
    let columnEnd: Int
}

struct TableJSON: Codable {
    let rows: [[TableCellJSON]]
}

struct ListItemJSON: Codable {
    let marker: String
    let text: String
}

struct ListJSON: Codable {
    let items: [ListItemJSON]
}

struct VisionResultJSON: Codable {
    let engine: String
    let confidence: Float
    let title: String?
    let text: String
    let paragraphs: [TextBlockJSON]
    let lines: [LineJSON]
    let words: [WordJSON]
    let tables: [TableJSON]
    let lists: [ListJSON]
}

enum VisionOCRError: Error, CustomStringConvertible {
    case fileNotFound(String)
    case noObservation

    var description: String {
        switch self {
        case .fileNotFound(let path):
            return "File not found: \(path)"
        case .noObservation:
            return "Vision did not return a document observation."
        }
    }
}

@main
struct VisionOCR {

    static func stderr(_ message: String) {
        if let data = (message + "\n").data(using: .utf8) {
            FileHandle.standardError.write(data)
        }
    }

    static func box(_ region: NormalizedRegion) -> BoxJSON {
        let r = region.boundingBox
        return BoxJSON(
            x: Double(r.origin.x),
            y: Double(r.origin.y),
            width: Double(r.width),
            height: Double(r.height)
        )
    }

    static func main() async {
        let args = CommandLine.arguments

        if args.count != 2 || args[1] == "--help" || args[1] == "-h" {
            stderr("visionocr — Apple Vision document OCR")
            stderr("")
            stderr("Usage:")
            stderr("  visionocr <image-file>")
            stderr("")
            stderr("Output:")
            stderr("  structured JSON on stdout")
            exit(args.count == 2 ? 0 : 2)
        }

        do {
            let path = args[1]
            let url = URL(fileURLWithPath: path)

            guard FileManager.default.fileExists(atPath: path) else {
                throw VisionOCRError.fileNotFound(path)
            }

            var request = RecognizeDocumentsRequest()
            request.textRecognitionOptions.automaticallyDetectLanguage = true
            request.textRecognitionOptions.useLanguageCorrection = true
            request.textRecognitionOptions.maximumCandidateCount = 3
            request.barcodeDetectionOptions.enabled = false

            let observations = try await request.perform(on: url)

            guard let observation = observations.first else {
                throw VisionOCRError.noObservation
            }

            let document = observation.document

            let paragraphJSON = document.paragraphs.map {
                TextBlockJSON(
                    text: $0.transcript,
                    bbox: box($0.boundingRegion)
                )
            }

            let lineJSON = document.text.lines.map { line in
                let candidate = line.topCandidates(1).first
                return LineJSON(
                    text: line.transcript,
                    confidence: candidate?.confidence,
                    bbox: box(line.boundingRegion),
                    isTitle: line.isTitle
                )
            }

            let wordJSON = (document.text.words ?? []).map { word in
                let candidates = word.topCandidates(3)

                return WordJSON(
                    text: word.transcript,
                    confidence: candidates.first?.confidence,
                    bbox: box(word.boundingRegion),
                    candidates: candidates.map {
                        CandidateJSON(
                            text: $0.string,
                            confidence: $0.confidence
                        )
                    }
                )
            }

            let tableJSON = document.tables.map { table in
                TableJSON(
                    rows: table.rows.map { row in
                        row.map { cell in
                            TableCellJSON(
                                text: cell.content.text.transcript,
                                rowStart: cell.rowRange.lowerBound,
                                rowEnd: cell.rowRange.upperBound,
                                columnStart: cell.columnRange.lowerBound,
                                columnEnd: cell.columnRange.upperBound
                            )
                        }
                    }
                )
            }

            let listJSON = document.lists.map { list in
                ListJSON(
                    items: list.items.map { item in
                        ListItemJSON(
                            marker: item.markerString,
                            text: item.itemString
                        )
                    }
                )
            }

            let result = VisionResultJSON(
                engine: "apple-vision",
                confidence: observation.confidence,
                title: document.title?.transcript,
                text: document.text.transcript,
                paragraphs: paragraphJSON,
                lines: lineJSON,
                words: wordJSON,
                tables: tableJSON,
                lists: listJSON
            )

            let encoder = JSONEncoder()
            encoder.outputFormatting = [
                .prettyPrinted,
                .sortedKeys,
                .withoutEscapingSlashes
            ]

            let data = try encoder.encode(result)
            FileHandle.standardOutput.write(data)
            FileHandle.standardOutput.write(Data("\n".utf8))

        } catch {
            stderr("visionocr error: \(error)")
            exit(1)
        }
    }
}
