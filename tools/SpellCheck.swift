import Foundation
import AppKit

let checker = NSSpellChecker.shared
let language = "ru_RU"

func clean(_ source: String) -> String {
    source
        .trimmingCharacters(in: .whitespacesAndNewlines)
        .trimmingCharacters(
            in: CharacterSet.punctuationCharacters.union(.symbols)
        )
}

for raw in CommandLine.arguments.dropFirst() {
    let word = clean(raw)

    if word.isEmpty {
        print("EMPTY")
        continue
    }

    let result = checker.checkSpelling(
        of: word,
        startingAt: 0,
        language: language,
        wrap: false,
        inSpellDocumentWithTag: 0,
        wordCount: nil
    )

    print(result.location == NSNotFound ? "VALID" : "INVALID")
}
