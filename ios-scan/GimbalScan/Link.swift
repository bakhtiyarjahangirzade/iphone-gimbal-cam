import Foundation

/// WebSocket link to the GimbalCam Windows app (ws://<pc>:8444/scanws?t=<token>).
///   phone -> PC  text: JSON (hello, pose, room, instruction, state, file_begin, file_end)
///               binary: [1][uuid16][4x4 f32][nV u32][nF u32][verts f32*3][faces u32*3]  mesh anchor
///                       [2][bytes]                                               file chunk
///                       [3][uuid16]                                              mesh anchor removed
///   PC -> phone  text: JSON commands {"cmd": "start"|"stop"|"mode"|"lang", ...}
final class Link: NSObject, ObservableObject, URLSessionWebSocketDelegate {
    @Published var connected = false
    @Published var host = ""
    @Published var paired = false

    var onCommand: (([String: Any]) -> Void)?
    var onConnected: (() -> Void)?

    private var session: URLSession!
    private var task: URLSessionWebSocketTask?
    private var url: URL?
    private var generation = 0
    private let lock = NSLock()
    private var inFlight = 0
    private var pingTimer: Timer?

    override init() {
        super.init()
        session = URLSession(configuration: .default, delegate: self, delegateQueue: OperationQueue())
        if let saved = UserDefaults.standard.string(forKey: "pairURL"), let u = URL(string: saved) {
            url = u
            host = u.host ?? ""
            paired = true
        }
    }

    /// Accepts the QR content: gimbalscan://connect?h=..&p=..&t=..  or the camera-mode https URL.
    @discardableResult
    func pair(with text: String) -> Bool {
        guard let comps = URLComponents(string: text.trimmingCharacters(in: .whitespacesAndNewlines)) else { return false }
        let items = Dictionary((comps.queryItems ?? []).map { ($0.name, $0.value ?? "") },
                               uniquingKeysWith: { first, _ in first })
        var h = items["h"] ?? ""
        var p = Int(items["p"] ?? "") ?? 8444
        let t = items["t"] ?? ""
        if comps.scheme == "https" || comps.scheme == "http" {   // camera-mode QR also works
            h = comps.host ?? ""
            p = 8444
        }
        guard !h.isEmpty, !t.isEmpty,
              let u = URL(string: "ws://\(h):\(p)/scanws?t=\(t)") else { return false }
        if let l = items["l"], !l.isEmpty { UserDefaults.standard.set(l, forKey: "lang") }
        UserDefaults.standard.set(u.absoluteString, forKey: "pairURL")
        DispatchQueue.main.async {
            self.url = u
            self.host = h
            self.paired = true
            self.reconnect()
        }
        return true
    }

    func unpair() {
        UserDefaults.standard.removeObject(forKey: "pairURL")
        generation += 1
        task?.cancel(with: .goingAway, reason: nil)
        paired = false
        connected = false
    }

    func reconnect() {
        generation += 1
        let gen = generation
        task?.cancel(with: .goingAway, reason: nil)
        guard let url else { return }
        let t = session.webSocketTask(with: url)
        t.maximumMessageSize = 64 << 20
        task = t
        t.resume()
        receive(t, gen)
        pingTimer?.invalidate()
        pingTimer = Timer.scheduledTimer(withTimeInterval: 4, repeats: true) { [weak self] _ in
            guard let self, let task = self.task else { return }
            task.sendPing { err in
                if err != nil { self.failed(gen) }
            }
        }
    }

    private func failed(_ gen: Int) {
        DispatchQueue.main.async {
            guard gen == self.generation else { return }
            self.connected = false
            self.lock.lock(); self.inFlight = 0; self.lock.unlock()
            DispatchQueue.main.asyncAfter(deadline: .now() + 2) {
                if gen == self.generation && self.paired { self.reconnect() }
            }
        }
    }

    private func receive(_ t: URLSessionWebSocketTask, _ gen: Int) {
        t.receive { [weak self] result in
            guard let self else { return }
            switch result {
            case .success(let msg):
                if case .string(let s) = msg, let d = s.data(using: .utf8),
                   let obj = (try? JSONSerialization.jsonObject(with: d)) as? [String: Any] {
                    DispatchQueue.main.async { self.onCommand?(obj) }
                }
                self.receive(t, gen)
            case .failure:
                self.failed(gen)
            }
        }
    }

    func urlSession(_ session: URLSession, webSocketTask: URLSessionWebSocketTask,
                    didOpenWithProtocol protocol: String?) {
        DispatchQueue.main.async {
            self.connected = true
            self.onConnected?()
        }
    }

    // MARK: - Sending

    var backlog: Int {
        lock.lock(); defer { lock.unlock() }
        return inFlight
    }

    func send(json: [String: Any]) {
        guard connected, let task,
              let d = try? JSONSerialization.data(withJSONObject: json),
              let s = String(data: d, encoding: .utf8) else { return }
        task.send(.string(s)) { _ in }
    }

    func send(data: Data, completion: ((Bool) -> Void)? = nil) {
        guard connected, let task else { completion?(false); return }
        lock.lock(); inFlight += data.count; lock.unlock()
        task.send(.data(data)) { [weak self] err in
            guard let self else { return }
            self.lock.lock(); self.inFlight -= data.count; self.lock.unlock()
            completion?(err == nil)
        }
    }

    /// Sends a file in 1 MB chunks, one at a time.
    func sendFile(_ url: URL, done: @escaping (Bool) -> Void) {
        guard let handle = try? FileHandle(forReadingFrom: url) else { done(false); return }
        let size = (try? FileManager.default.attributesOfItem(atPath: url.path)[.size] as? Int) ?? 0
        send(json: ["type": "file_begin", "name": url.lastPathComponent, "size": size])
        func next() {
            let chunk = handle.readData(ofLength: 1 << 20)
            if chunk.isEmpty {
                try? handle.close()
                send(json: ["type": "file_end", "name": url.lastPathComponent])
                done(true)
                return
            }
            var msg = Data([2])
            msg.append(chunk)
            send(data: msg) { ok in
                if ok { next() } else { try? handle.close(); done(false) }
            }
        }
        next()
    }
}
