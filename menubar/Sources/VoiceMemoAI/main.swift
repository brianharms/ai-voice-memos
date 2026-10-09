import SwiftUI
import Foundation

// AI Voice Memos — menu-bar app.
// A thin native shell over the existing daemon + web configurator. It shells out
// to the `vma` control script (launchd + config server) so the user never touches
// a terminal, and polls the daemon's status.json to show a live glyph.

let HOME = FileManager.default.homeDirectoryForCurrentUser
// Where the repo lives: `vma load` writes it to ~/.voice-memo-ai/project-path.
// Falls back to the original location when that file is absent.
let PROJECT: URL = {
    let f = HOME.appendingPathComponent(".voice-memo-ai/project-path")
    if let s = try? String(contentsOf: f, encoding: .utf8) {
        let p = s.trimmingCharacters(in: .whitespacesAndNewlines)
        if !p.isEmpty, FileManager.default.fileExists(atPath: p + "/daemon/vma") {
            return URL(fileURLWithPath: p)
        }
    }
    return HOME.appendingPathComponent("Desktop/Claude Projects/voice-memo-ai")
}()
let VMA = PROJECT.appendingPathComponent("daemon/vma")
let STATUS_FILE = HOME.appendingPathComponent(".voice-memo-ai/status.json")
let PROCESSING_FILE = HOME.appendingPathComponent(".voice-memo-ai/processing.json")
// The Mini's Configurator owns the default model + effort (GET/POST /model). On the
// Mini this is the Configurator itself; on the MacBook it's the standing tunnel to it.
let CONFIGURATOR = URL(string: "http://127.0.0.1:8787")!

struct Choice: Identifiable, Hashable { let id: String; let label: String }

struct ModelSettings {
    var model = "", effort = ""
    var models: [Choice] = [], efforts: [Choice] = []
    func label(_ list: [Choice], _ id: String) -> String { list.first { $0.id == id }?.label ?? id }
}

func parseChoices(_ any: Any?) -> [Choice] {
    ((any as? [[String: Any]]) ?? []).compactMap { d in
        guard let i = d["id"] as? String, let l = d["label"] as? String else { return nil }
        return Choice(id: i, label: l)
    }
}

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
    var paused: Bool = false
    var memoName: String? = nil      // the memo being worked on (or waiting behind a pause)
    var parkedCount: Int = 0
}

func readStatus() -> DaemonStatus {
    var s = DaemonStatus()
    guard let data = try? Data(contentsOf: STATUS_FILE),
          let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
    else { return s }
    s.running = (obj["running"] as? Bool) ?? false
    s.claudeOK = (obj["claude_ok"] as? Bool) ?? false
    s.paused = (obj["paused"] as? Bool) ?? false
    s.parkedCount = (obj["parked"] as? [Any])?.count ?? 0
    if let d = try? Data(contentsOf: PROCESSING_FILE),
       let p = try? JSONSerialization.jsonObject(with: d) as? [String: Any] {
        s.memoName = p["memo_name"] as? String
    }
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
    @Published var settings: ModelSettings? = nil   // nil = Configurator unreachable
    private var token: String? = nil

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
        // poll the heartbeat every 2s on the main actor; the model setting every 10s
        Task { [weak self] in
            var tick = 0
            while true {
                if tick % 5 == 0 { await self?.refreshSettings() }
                try? await Task.sleep(nanoseconds: 2_000_000_000)
                self?.refresh()
                if self == nil { break }
                tick += 1
            }
        }
    }

    func refreshSettings() async {
        var req = URLRequest(url: CONFIGURATOR.appendingPathComponent("model"))
        req.timeoutInterval = 3
        guard let (data, resp) = try? await URLSession.shared.data(for: req),
              (resp as? HTTPURLResponse)?.statusCode == 200,
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { settings = nil; return }
        settings = ModelSettings(model: obj["model"] as? String ?? "",
                                 effort: obj["effort"] as? String ?? "",
                                 models: parseChoices(obj["models"]),
                                 efforts: parseChoices(obj["efforts"]))
    }

    // key is "model" or "effort"; value one of the Configurator's offered ids
    func setSetting(_ key: String, _ value: String) {
        if key == "model" { settings?.model = value } else { settings?.effort = value }  // optimistic
        Task { [weak self] in await self?.post([key: value]) }
    }

    private func post(_ body: [String: String]) async {
        // writes need the per-run token the Configurator injects into the page it
        // serves; a 403 means it restarted with a new one, so fetch it and retry once
        for attempt in 0..<2 {
            if token == nil || attempt == 1 { token = await fetchToken() }
            guard let t = token else { break }
            var req = URLRequest(url: CONFIGURATOR.appendingPathComponent("model"))
            req.httpMethod = "POST"
            req.timeoutInterval = 5
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.setValue(t, forHTTPHeaderField: "X-VMA-Token")
            req.httpBody = try? JSONSerialization.data(withJSONObject: body)
            if let (_, resp) = try? await URLSession.shared.data(for: req),
               (resp as? HTTPURLResponse)?.statusCode == 200 { break }
        }
        await refreshSettings()
    }

    private func fetchToken() async -> String? {
        guard let (data, _) = try? await URLSession.shared.data(from: CONFIGURATOR),
              let html = String(data: data, encoding: .utf8),
              let re = try? NSRegularExpression(pattern: #"name="vma-token" content="([^"]+)""#),
              let m = re.firstMatch(in: html, range: NSRange(html.startIndex..., in: html)),
              let r = Range(m.range(at: 1), in: html)
        else { return nil }
        return String(html[r])
    }

    func refresh() { status = readStatus() }

    func enable()  { runVMA("load");   bump() }
    func disable() { runVMA("unload"); bump() }
    func openUI()  { runVMA("config") }
    func pause()   { runVMA("pause");  bump() }
    func resume()  { runVMA("resume"); bump() }
    func cancelCurrent() { runVMA("cancel"); bump() }
    func laterCurrent()  { runVMA("later");  bump() }

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
        MenuBarExtra("AI Voice Memos", systemImage: glyph) {
            // status line
            if model.status.running {
                let tick = model.status.lastPollSecondsAgo.map { "\($0)s ago" } ?? ""
                Text("● Watching voice memos" + (tick.isEmpty ? "" : "  ·  \(tick)"))
                Text(model.status.claudeOK ? "Claude connected" : "Claude not connected")
                    .font(.caption)
            } else {
                Text("○ Not running")
            }

            if model.status.running, let name = model.status.memoName {
                Divider()
                Text("Working on: \(name)").font(.caption)
                Button("Save This Memo for Later") { model.laterCurrent() }
                Button("Cancel This Memo") { model.cancelCurrent() }
            }
            if model.status.running {
                Divider()
                if model.status.paused {
                    Button("Resume Processing") { model.resume() }
                } else {
                    Button("Pause Processing") { model.pause() }
                }
                if model.status.parkedCount > 0 {
                    Text("\(model.status.parkedCount) saved for later — run them from the Configurator")
                        .font(.caption)
                }
            }

            // default model + effort for every run (same setting as the Configurator)
            Divider()
            if let s = model.settings, !s.models.isEmpty {
                Picker("Model: \(s.label(s.models, s.model))",
                       selection: Binding(get: { s.model }, set: { model.setSetting("model", $0) })) {
                    ForEach(s.models) { Text($0.label).tag($0.id) }
                }
                .pickerStyle(.menu)
                Picker("Effort: \(s.label(s.efforts, s.effort))",
                       selection: Binding(get: { s.effort }, set: { model.setSetting("effort", $0) })) {
                    ForEach(s.efforts) { Text($0.label).tag($0.id) }
                }
                .pickerStyle(.menu)
            } else {
                Text("Model: Configurator not reachable").font(.caption)
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
