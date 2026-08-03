// swift-tools-version:6.0
import PackageDescription

let package = Package(
    name: "VoiceMemoAI",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(
            name: "VoiceMemoAI",
            path: "Sources/VoiceMemoAI"
        )
    ]
)
