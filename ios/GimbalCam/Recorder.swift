import AVFoundation

/// Records the camera frames to a high-bitrate HEVC .mov in the app's Documents folder.
final class Recorder {
    private let lock = NSLock()
    private var writer: AVAssetWriter?
    private var input: AVAssetWriterInput?
    private var width = 1920
    private var height = 1080
    private var fps = 30

    func configure(width: Int, height: Int, fps: Int) {
        lock.lock()
        self.width = width
        self.height = height
        self.fps = fps
        lock.unlock()
    }

    func start() throws {
        lock.lock()
        defer { lock.unlock() }

        let docs = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
        let df = DateFormatter()
        df.dateFormat = "yyyyMMdd_HHmmss"
        let url = docs.appendingPathComponent("GimbalCam_\(df.string(from: Date())).mov")

        let w = try AVAssetWriter(outputURL: url, fileType: .mov)
        // ~20 Mbps at 1080p30, scaled by pixel rate; capped at 150 Mbps.
        let pixelRate = Double(width * height * fps)
        let bitrate = min(150_000_000, Int(20_000_000 * pixelRate / (1920 * 1080 * 30)))
        let settings: [String: Any] = [
            AVVideoCodecKey: AVVideoCodecType.hevc,
            AVVideoWidthKey: width,
            AVVideoHeightKey: height,
            AVVideoCompressionPropertiesKey: [
                AVVideoAverageBitRateKey: bitrate,
                AVVideoExpectedSourceFrameRateKey: fps,
            ],
        ]
        let i = AVAssetWriterInput(mediaType: .video, outputSettings: settings)
        i.expectsMediaDataInRealTime = true
        guard w.canAdd(i) else {
            throw NSError(domain: "Recorder", code: 1, userInfo: [NSLocalizedDescriptionKey: "Video girişi eklenemedi"])
        }
        w.add(i)
        writer = w
        input = i
    }

    /// Called on the capture queue for every frame.
    func append(_ sampleBuffer: CMSampleBuffer) {
        lock.lock()
        defer { lock.unlock() }
        guard let writer, let input else { return }

        if writer.status == .unknown {
            guard writer.startWriting() else { return }
            writer.startSession(atSourceTime: CMSampleBufferGetPresentationTimeStamp(sampleBuffer))
        }
        if writer.status == .writing, input.isReadyForMoreMediaData {
            input.append(sampleBuffer)
        }
    }

    func stop(completion: @escaping (URL?) -> Void) {
        lock.lock()
        let w = writer
        let i = input
        writer = nil
        input = nil
        lock.unlock()

        guard let w, let i, w.status == .writing else {
            completion(nil)
            return
        }
        i.markAsFinished()
        w.finishWriting {
            completion(w.status == .completed ? w.outputURL : nil)
        }
    }
}
