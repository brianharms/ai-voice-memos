import SwiftUI
import Foundation

// Voice Memo AI — menu-bar app.
// A thin native shell over the existing daemon + web configurator. It shells out
// to the `vma` control script (launchd + config server) so the user never touches
// a terminal, and polls the daemon's status.json to show a live glyph.

let HOME = FileManager.default.homeDirectoryForCurrentUser
// repo root: override with VMA_PROJECT_DIR if you cloned somewhere else
let PROJECT = URL(fileURLWithPath: ProcessInfo.processInfo.environment["VMA_PROJECT_DIR"]
    ?? HOME.appendingPathComponent("Desktop/Claude Projects/ai-voice-memos").path)
let VMA = PROJECT.appendingPathComponent("daemon/vma")
let STATUS_FILE = HOME.appendingPathComponent(".voice-memo-ai/status.json")

// Run a `vma` subcommand (load/unload/config) detached; never blocks the UI.
func runVMA(_ arg: String) {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: "/bin/zsh")
    // -l so login PATH (python3, etc.) resolves; call the script by absolute path
    p.arguments = ["-lc", "'\(VMA.path)' \(arg)"]
    try? p.run()
}

struct DaemonStatus {
    var running: Bool = false
    var claudeOK: Bool = false
    var lastPollSecondsAgo: Int? = nil
}

func readStatus() -> DaemonStatus {
    var s = DaemonStatus()
    guard let data = try? Data(contentsOf: STATUS_FILE),
          let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
    else { return s }
    s.running = (obj["running"] as? Bool) ?? false
    s.claudeOK = (obj["claude_ok"] as? Bool) ?? false
    // verify the pid is actually alive (heartbeat could be stale)
    if let pid = obj["pid"] as? Int, s.running {
        s.running = (kill(pid_t(pid), 0) == 0)
    }
    if let lp = obj["last_poll"] as? String {
        let fmt = ISO8601DateFormatter()
        fmt.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let d = fmt.date(from: lp) ?? ISO8601DateFormatter().date(from: lp) {
            s.lastPollSecondsAgo = max(0, Int(Date().timeIntervalSince(d)))
        }
    }
    return s
}

@MainActor
final class Model: ObservableObject {
    @Published var status = DaemonStatus()

    init() {
        refresh()
        // SELF-HEAL AT LAUNCH: this app sits in Login Items (login items launch
        // reliably; launchd's RunAtLoad can be pended by macOS). If the daemon
        // isn't running shortly after login, load it — this is the guarantee
        // that a reboot always comes back watching.
        Task { [weak self] in
            try? await Task.sleep(nanoseconds: 5_000_000_000)
            guard let self else { return }
            // the installed plist is the "user wants it ON" signal — vma unload
            // removes it, so a deliberate Disable is never overridden here.
            let plist = HOME.appendingPathComponent(
                "Library/LaunchAgents/industries.ritual.voice-memo-ai.plist")
            if !self.status.running && FileManager.default.fileExists(atPath: plist.path) {
                runVMA("load")
                self.bump()
            }
        }
        // poll the heartbeat every 2s on the main actor
        Task { [weak self] in
            while true {
                try? await Task.sleep(nanoseconds: 2_000_000_000)
                self?.refresh()
                if self == nil { break }
            }
        }
    }

    func refresh() { status = readStatus() }

    func enable()  { runVMA("load");   bump() }
    func disable() { runVMA("unload"); bump() }
    func openUI()  { runVMA("config") }

    // give the daemon a moment to update its heartbeat, then re-read a few times
    private func bump() {
        Task { [weak self] in
            for ms in [600, 1400, 3000] {
                try? await Task.sleep(nanoseconds: UInt64(ms) * 1_000_000)
                self?.refresh()
            }
        }
    }
}

@main
struct VoiceMemoAIApp: App {
    @StateObject private var model = Model()

    var glyph: String {
        model.status.running ? "waveform.circle.fill" : "waveform.circle"
    }

    var body: some Scene {
        MenuBarExtra("Voice Memo AI", systemImage: glyph) {
            // status line
            if model.status.running {
                let tick = model.status.lastPollSecondsAgo.map { "\($0)s ago" } ?? ""
                Text("● Watching voice memos" + (tick.isEmpty ? "" : "  ·  \(tick)"))
                Text(model.status.claudeOK ? "Claude connected" : "Claude not connected")
                    .font(.caption)
            } else {
                Text("○ Not running")
            }

            Divider()

            if model.status.running {
                Button("Disable") { model.disable() }
            } else {
                Button("Enable") { model.enable() }
            }
            Button("Open Configurator…") { model.openUI() }

            Divider()
            Button("Quit") { NSApplication.shared.terminate(nil) }
        }
        .menuBarExtraStyle(.menu)
    }
}
