import Foundation
import VideoToolbox
import CoreMedia

/// Hardware H.264 encoder producing Annex-B access units (SPS/PPS prepended on keyframes).
final class VideoEncoder {
    var onEncoded: ((Data, Bool, CMTime) -> Void)?

    private var session: VTCompressionSession?
    private var width: Int32 = 0
    private var height: Int32 = 0
    private var forceKey = true
    private let lock = NSLock()

    func configure(width: Int32, height: Int32, fps: Int, bitrateMbps: Double) {
        lock.lock()
        defer { lock.unlock() }

        if let s = session {
            VTCompressionSessionCompleteFrames(s, untilPresentationTimeStamp: .invalid)
            VTCompressionSessionInvalidate(s)
            session = nil
        }

        let spec: [CFString: Any] = [
            kVTVideoEncoderSpecification_EnableLowLatencyRateControl: true,
        ]
        var newSession: VTCompressionSession?
        let err = VTCompressionSessionCreate(
            allocator: nil,
            width: width,
            height: height,
            codecType: kCMVideoCodecType_H264,
            encoderSpecification: spec as CFDictionary,
            imageBufferAttributes: nil,
            compressedDataAllocator: nil,
            outputCallback: nil,
            refcon: nil,
            compressionSessionOut: &newSession)
        guard err == noErr, let s = newSession else {
            print("VTCompressionSessionCreate failed: \(err)")
            return
        }

        let bitrate = Int(bitrateMbps * 1_000_000)
        VTSessionSetProperty(s, key: kVTCompressionPropertyKey_RealTime, value: kCFBooleanTrue)
        VTSessionSetProperty(s, key: kVTCompressionPropertyKey_ProfileLevel, value: kVTProfileLevel_H264_High_AutoLevel)
        VTSessionSetProperty(s, key: kVTCompressionPropertyKey_AllowFrameReordering, value: kCFBooleanFalse)
        VTSessionSetProperty(s, key: kVTCompressionPropertyKey_AverageBitRate, value: bitrate as CFNumber)
        VTSessionSetProperty(s, key: kVTCompressionPropertyKey_ExpectedFrameRate, value: fps as CFNumber)
        VTSessionSetProperty(s, key: kVTCompressionPropertyKey_MaxKeyFrameInterval, value: (fps * 2) as CFNumber)
        VTSessionSetProperty(s, key: kVTCompressionPropertyKey_MaxKeyFrameIntervalDuration, value: 2 as CFNumber)
        VTCompressionSessionPrepareToEncodeFrames(s)

        session = s
        self.width = width
        self.height = height
        forceKey = true
    }

    func forceKeyframe() {
        lock.lock()
        forceKey = true
        lock.unlock()
    }

    func encode(_ sampleBuffer: CMSampleBuffer) {
        guard let pixelBuffer = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }

        lock.lock()
        let s = session
        let w = width, h = height
        let key = forceKey
        forceKey = false
        lock.unlock()

        guard let s,
              CVPixelBufferGetWidth(pixelBuffer) == Int(w),
              CVPixelBufferGetHeight(pixelBuffer) == Int(h) else { return }

        let pts = CMSampleBufferGetPresentationTimeStamp(sampleBuffer)
        let props: CFDictionary? = key
            ? [kVTEncodeFrameOptionKey_ForceKeyFrame: true] as CFDictionary
            : nil

        VTCompressionSessionEncodeFrame(
            s,
            imageBuffer: pixelBuffer,
            presentationTimeStamp: pts,
            duration: .invalid,
            frameProperties: props,
            infoFlagsOut: nil
        ) { [weak self] status, _, buffer in
            guard status == noErr, let buffer else { return }
            self?.emit(buffer)
        }
    }

    private func emit(_ buffer: CMSampleBuffer) {
        let startCode: [UInt8] = [0, 0, 0, 1]

        var isKey = true
        if let attachments = CMSampleBufferGetSampleAttachmentsArray(buffer, createIfNecessary: false) as? [[CFString: Any]],
           let first = attachments.first,
           let notSync = first[kCMSampleAttachmentKey_NotSync] as? Bool {
            isKey = !notSync
        }

        var out = Data()
        if isKey, let fmt = CMSampleBufferGetFormatDescription(buffer) {
            var count = 0
            CMVideoFormatDescriptionGetH264ParameterSetAtIndex(
                fmt, parameterSetIndex: 0, parameterSetPointerOut: nil,
                parameterSetSizeOut: nil, parameterSetCountOut: &count, nalUnitHeaderLengthOut: nil)
            for i in 0..<count {
                var ptr: UnsafePointer<UInt8>?
                var size = 0
                CMVideoFormatDescriptionGetH264ParameterSetAtIndex(
                    fmt, parameterSetIndex: i, parameterSetPointerOut: &ptr,
                    parameterSetSizeOut: &size, parameterSetCountOut: nil, nalUnitHeaderLengthOut: nil)
                if let ptr {
                    out.append(contentsOf: startCode)
                    out.append(ptr, count: size)
                }
            }
        }

        guard let block = CMSampleBufferGetDataBuffer(buffer) else { return }
        let total = CMBlockBufferGetDataLength(block)
        var bytes = [UInt8](repeating: 0, count: total)
        CMBlockBufferCopyDataBytes(block, atOffset: 0, dataLength: total, destination: &bytes)

        // AVCC (4-byte length prefixes) -> Annex-B start codes
        var offset = 0
        while offset + 4 <= total {
            let len = Int(UInt32(bytes[offset]) << 24 | UInt32(bytes[offset + 1]) << 16 |
                          UInt32(bytes[offset + 2]) << 8 | UInt32(bytes[offset + 3]))
            offset += 4
            guard len > 0, offset + len <= total else { break }
            out.append(contentsOf: startCode)
            out.append(contentsOf: bytes[offset..<(offset + len)])
            offset += len
        }

        onEncoded?(out, isKey, CMSampleBufferGetPresentationTimeStamp(buffer))
    }
}
