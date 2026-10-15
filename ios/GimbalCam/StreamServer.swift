import Foundation
import Network
import UIKit

/// TCP server the Windows app connects to (over USB via usbmuxd, or over Wi-Fi).
///
/// Every message is framed as: [type: UInt8][length: UInt32 big-endian][payload]
///   type 1  video   payload = [flags: UInt8 (bit0 = keyframe)][pts µs: Int64 BE][H.264 Annex-B access unit]
///   type 2  json    UTF-8 JSON object (both directions; PC -> phone carries commands)
///   type 3  file begin  JSON {"name": String, "size": Int}
///   type 4  file chunk  raw bytes
///   type 5  file end    JSON {"name": String}
final class StreamServer {
    static let port: UInt16 = 5005

    var onClientsChanged: ((Int) -> Void)?
    var onNeedKeyframe: (() -> Void)?
    var onCommand: (([String: Any]) -> Void)?
    var helloProvider: (() -> [String: Any])?

    private(set) var clientCount = 0

    private final class Client {
        let conn: NWConnection
        var waitingForKey = true
        var pending = 0
        init(_ c: NWConnection) { conn = c }
    }

    private var listener: NWListener?
    private var clients: [ObjectIdentifier: Client] = [:]
    private let queue = DispatchQueue(label: "net.server", qos: .userInteractive)
    private let maxPendingFrames = 20

    func start() {
        let tcp = NWProtocolTCP.Options()
        tcp.noDelay = true
        let params = NWParameters(tls: nil, tcp: tcp)
        params.allowLocalEndpointReuse = true

        do {
            let l = try NWListener(using: params, on: NWEndpoint.Port(rawValue: StreamServer.port)!)
            l.service = NWListener.Service(name: UIDevice.current.name, type: "_gimbalcam._tcp")
            l.newConnectionHandler = { [weak self] c in self?.accept(c) }
            l.stateUpdateHandler = { [weak self] state in
                if case .failed = state {
                    // Restart after a short delay (e.g. network change).
                    self?.listener?.cancel()
                    self?.queue.asyncAfter(deadline: .now() + 1) { self?.start() }
                }
            }
            l.start(queue: queue)
            listener = l
        } catch {
            print("Listener error: \(error)")
        }
    }

    private func accept(_ conn: NWConnection) {
        let client = Client(conn)
        let id = ObjectIdentifier(client)
        conn.stateUpdateHandler = { [weak self] state in
            guard let self else { return }
            switch state {
            case .ready:
                self.clients[id] = client
                self.updateCount()
                if let hello = self.helloProvider?() { self.send(json: hello, to: client) }
                self.onNeedKeyframe?()
                self.receive(client)
            case .failed, .cancelled:
                if self.clients.removeValue(forKey: id) != nil { self.updateCount() }
            default:
                break
            }
        }
        conn.start(queue: queue)
    }

    private func updateCount() {
        clientCount = clients.count
        onClientsChanged?(clients.count)
    }

    // MARK: - Sending

    func broadcastVideo(_ data: Data, keyframe: Bool, ptsMicros: Int64) {
        queue.async {
            for client in self.clients.values {
                if client.pending > self.maxPendingFrames {
                    // Link is congested: drop frames until the next keyframe.
                    if !client.waitingForKey {
                        client.waitingForKey = true
                        self.onNeedKeyframe?()
                    }
                    continue
                }
                if client.waitingForKey {
                    guard keyframe else { continue }
                    client.waitingForKey = false
                }
                var payload = Data([keyframe ? 1 : 0])
                var pts = ptsMicros.bigEndian
                payload.append(Data(bytes: &pts, count: 8))
                payload.append(data)
                self.send(type: 1, payload: payload, to: client)
            }
        }
    }

    func broadcastJSON(_ obj: [String: Any]) {
        queue.async {
            for client in self.clients.values { self.send(json: obj, to: client) }
        }
    }

    private func send(json obj: [String: Any], to client: Client) {
        guard let data = try? JSONSerialization.data(withJSONObject: obj) else { return }
        send(type: 2, payload: data, to: client)
    }

    private func send(type: UInt8, payload: Data, to client: Client, completion: (() -> Void)? = nil) {
        var frame = Data([type])
        var len = UInt32(payload.count).bigEndian
        frame.append(Data(bytes: &len, count: 4))
        frame.append(payload)
        client.pending += 1
        client.conn.send(content: frame, completion: .contentProcessed { _ in
            client.pending -= 1
            completion?()
        })
    }

    // MARK: - File transfer (recordings -> PC)

    func sendFile(_ url: URL) {
        queue.async {
            guard let client = self.clients.values.first,
                  let handle = try? FileHandle(forReadingFrom: url) else { return }
            let size = (try? FileManager.default.attributesOfItem(atPath: url.path)[.size] as? Int) ?? 0
            let name = url.lastPathComponent
            guard let begin = try? JSONSerialization.data(withJSONObject: ["name": name, "size": size]) else { return }
            self.send(type: 3, payload: begin, to: client)

            func sendNext() {
                let chunk = handle.readData(ofLength: 1 << 20)
                if chunk.isEmpty {
                    try? handle.close()
                    if let end = try? JSONSerialization.data(withJSONObject: ["name": name]) {
                        self.send(type: 5, payload: end, to: client)
                    }
                    return
                }
                self.send(type: 4, payload: chunk, to: client) {
                    self.queue.async { sendNext() }
                }
            }
            sendNext()
        }
    }

    // MARK: - Receiving commands

    private func receive(_ client: Client) {
        client.conn.receive(minimumIncompleteLength: 5, maximumLength: 5) { [weak self] header, _, isComplete, error in
            guard let self else { return }
            guard let header, header.count == 5, error == nil else {
                if isComplete || error != nil { client.conn.cancel() } else { self.receive(client) }
                return
            }
            let bytes = [UInt8](header)
            let type = bytes[0]
            let len = Int(bytes[1]) << 24 | Int(bytes[2]) << 16 | Int(bytes[3]) << 8 | Int(bytes[4])
            guard len >= 0, len < 1 << 20 else { client.conn.cancel(); return }
            if len == 0 { self.receive(client); return }

            client.conn.receive(minimumIncompleteLength: len, maximumLength: len) { body, _, done, err in
                if let body, type == 2,
                   let obj = (try? JSONSerialization.jsonObject(with: body)) as? [String: Any] {
                    DispatchQueue.main.async { self.onCommand?(obj) }
                }
                if done || err != nil { client.conn.cancel(); return }
                self.receive(client)
            }
        }
    }
}

enum NetworkInfo {
    /// IPv4 address of the Wi-Fi interface (en0), if any.
    static func wifiAddress() -> String? {
        var result: String?
        var ifaddr: UnsafeMutablePointer<ifaddrs>?
        guard getifaddrs(&ifaddr) == 0, let first = ifaddr else { return nil }
        defer { freeifaddrs(ifaddr) }
        for ptr in sequence(first: first, next: { $0.pointee.ifa_next }) {
            let iface = ptr.pointee
            guard let addr = iface.ifa_addr, addr.pointee.sa_family == UInt8(AF_INET) else { continue }
            if String(cString: iface.ifa_name) == "en0" {
                var host = [CChar](repeating: 0, count: Int(NI_MAXHOST))
                getnameinfo(addr, socklen_t(addr.pointee.sa_len), &host, socklen_t(host.count), nil, 0, NI_NUMERICHOST)
                result = String(cString: host)
            }
        }
        return result
    }
}
