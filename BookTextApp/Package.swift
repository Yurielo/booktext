// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "BookText",
    platforms: [.macOS(.v14)],
    products: [
        .library(name: "BookTextCore", targets: ["BookTextCore"]),
        .executable(name: "BookText", targets: ["BookTextApp"]),
        .executable(name: "BookTextProtocolTests", targets: ["BookTextProtocolTests"]),
    ],
    targets: [
        .target(name: "BookTextCore", path: "Sources/BookTextCore"),
        .executableTarget(name: "BookTextApp", dependencies: ["BookTextCore"], path: "Sources/BookTextApp"),
        .executableTarget(name: "BookTextProtocolTests", dependencies: ["BookTextCore"], path: "Sources/BookTextProtocolTests"),
    ]
)
