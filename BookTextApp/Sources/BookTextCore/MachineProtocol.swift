import Foundation

public let machineProtocolVersion = "1.1"

public enum MachineEventType: String, Codable, CaseIterable, Sendable {
    case jobStarted = "job_started", phaseStarted = "phase_started", progress, warning
    case artifactCreated = "artifact_created", reviewSummary = "review_summary"
    case phaseCompleted = "phase_completed", jobCompleted = "job_completed"
    case jobFailed = "job_failed", jobCancelled = "job_cancelled", unknown
    case chunkPlanCreated = "chunk_plan_created", chunkStarted = "chunk_started"
    case chunkProgress = "chunk_progress", chunkCompleted = "chunk_completed"
    case chunkFailed = "chunk_failed", chunkRetry = "chunk_retry", jobResumed = "job_resumed"
    case assemblyStarted = "assembly_started", assemblyCompleted = "assembly_completed"
    case cleanupStarted = "cleanup_started", cleanupProgress = "cleanup_progress"
    case cleanupCompleted = "cleanup_completed", cleanupWarning = "cleanup_warning"
    case pipelineStarted = "pipeline_started", stageStarted = "stage_started"
    case stageProgress = "stage_progress", stageCompleted = "stage_completed"
    case stageFailed = "stage_failed", pipelinePaused = "pipeline_paused"
    case pipelineResumed = "pipeline_resumed", pipelineCompleted = "pipeline_completed"

    public init(from decoder: Decoder) throws {
        let value = try decoder.singleValueContainer().decode(String.self)
        self = MachineEventType(rawValue: value) ?? .unknown
    }
}

public enum JSONValue: Codable, Sendable, Equatable {
    case string(String), number(Double), bool(Bool), object([String: JSONValue]), array([JSONValue]), null
    public init(from decoder: Decoder) throws {
        let box = try decoder.singleValueContainer()
        if box.decodeNil() { self = .null }
        else if let value = try? box.decode(Bool.self) { self = .bool(value) }
        else if let value = try? box.decode(Double.self) { self = .number(value) }
        else if let value = try? box.decode(String.self) { self = .string(value) }
        else if let value = try? box.decode([String: JSONValue].self) { self = .object(value) }
        else { self = .array(try box.decode([JSONValue].self)) }
    }
    public func encode(to encoder: Encoder) throws {
        var box = encoder.singleValueContainer()
        switch self { case .string(let x): try box.encode(x); case .number(let x): try box.encode(x); case .bool(let x): try box.encode(x); case .object(let x): try box.encode(x); case .array(let x): try box.encode(x); case .null: try box.encodeNil() }
    }
    public var object: [String: JSONValue]? { if case .object(let value) = self { value } else { nil } }
    public var values: [JSONValue]? { if case .array(let value) = self { value } else { nil } }
    public var string: String? { if case .string(let value) = self { value } else { nil } }
    public var integer: Int? { if case .number(let value) = self { Int(value) } else { nil } }
    public var bool: Bool? { if case .bool(let value) = self { value } else { nil } }
}

public struct MachineEvent: Codable, Sendable, Equatable {
    public let protocolVersion: String
    public let jobID: String
    public let eventType: MachineEventType
    public let timestamp: String
    public let phase: String?
    public let current: Int?
    public let total: Int?
    public let page: Int?
    public let code: String
    public let message: String
    public let details: JSONValue
    enum CodingKeys: String, CodingKey { case protocolVersion = "protocol_version", jobID = "job_id", eventType = "event_type", timestamp, phase, current, total, page, code, message, details }
    public var supported: Bool { protocolVersion.split(separator: ".").first == machineProtocolVersion.split(separator: ".").first }
}

public enum JobPresentationState: Equatable, Sendable { case idle, preflighting, ready, running, cancelling, cancelled, completed, failed }

public struct JobPresentation: Sendable, Equatable {
    public var state: JobPresentationState = .idle
    public var jobID: String?
    public var phase: String?
    public var current: Int?
    public var total: Int?
    public var page: Int?
    public var warnings: [String] = []
    public var errorCode: String?
    public init(state: JobPresentationState = .idle, jobID: String? = nil, phase: String? = nil, current: Int? = nil, total: Int? = nil, page: Int? = nil, warnings: [String] = [], errorCode: String? = nil) {
        self.state = state; self.jobID = jobID; self.phase = phase; self.current = current; self.total = total; self.page = page; self.warnings = warnings; self.errorCode = errorCode
    }
    public mutating func consume(_ event: MachineEvent) {
        guard event.supported else { state = .failed; errorCode = "unsupported_protocol"; return }
        jobID = event.jobID; phase = event.phase ?? phase; current = event.current ?? current; total = event.total ?? total; page = event.page ?? page
        switch event.eventType {
        case .jobStarted, .pipelineStarted, .pipelineResumed, .stageStarted, .stageProgress: state = .running
        case .warning: warnings.append(event.message)
        case .jobCompleted: state = .completed
        case .jobFailed, .stageFailed: state = .failed; errorCode = event.code
        case .jobCancelled, .pipelinePaused: state = .cancelled
        case .pipelineCompleted: state = .completed
        default: break
        }
    }
}

public struct CoreConfiguration: Sendable, Equatable {
    public var scriptPath: String
    public init(scriptPath: String) { self.scriptPath = scriptPath }
    public static var defaultValue: CoreConfiguration {
        let preferred = ProcessInfo.processInfo.environment["BOOKTEXT_CORE_PATH"]
        let home = FileManager.default.homeDirectoryForCurrentUser.path + "/AITools/booktext/booktext.py"
        return CoreConfiguration(scriptPath: preferred ?? home)
    }
    public func command(_ machineArguments: [String]) -> (URL, [String]) {
        // Process receives an executable plus argv; never a shell string.
        (URL(fileURLWithPath: "/usr/bin/env"), ["python3", scriptPath, "machine"] + machineArguments)
    }
}

public final class CoreProcessBridge: @unchecked Sendable {
    private var process: Process?
    public let configuration: CoreConfiguration
    public init(configuration: CoreConfiguration) { self.configuration = configuration }
    public func start(arguments: [String], event: @escaping @Sendable (MachineEvent) -> Void, stderr: @escaping @Sendable (String) -> Void, finished: @escaping @Sendable (Int32) -> Void) throws {
        guard FileManager.default.isReadableFile(atPath: configuration.scriptPath) else { throw NSError(domain: "BookText", code: 2, userInfo: [NSLocalizedDescriptionKey: "BookText Core script is unavailable at \(configuration.scriptPath)"]) }
        let process = Process(); let output = Pipe(); let errors = Pipe(); let command = configuration.command(arguments)
        // LaunchServices does not inherit the interactive shell PATH.  Keep
        // Homebrew's standard locations available to the local Core without
        // consulting a shell command or accepting an arbitrary executable.
        var environment = ProcessInfo.processInfo.environment
        let standardPath = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
        let inherited = (environment["PATH"] ?? "").split(separator: ":").map(String.init)
        environment["PATH"] = Array(Set(standardPath + inherited)).sorted().joined(separator: ":")
        process.executableURL = command.0; process.arguments = command.1; process.environment = environment; process.standardOutput = output; process.standardError = errors
        let decoder = JSONDecoder(); let buffer = JSONLBuffer()
        let consumeOutput: @Sendable (Data) -> Void = { received in
            guard !received.isEmpty else { return }
            for line in buffer.append(received) {
                if let decoded = try? decoder.decode(MachineEvent.self, from: line) { event(decoded) }
            }
        }
        output.fileHandleForReading.readabilityHandler = { handle in
            consumeOutput(handle.availableData)
        }
        errors.fileHandleForReading.readabilityHandler = { handle in
            let data = handle.availableData
            if !data.isEmpty, let text = String(data: data, encoding: .utf8) { stderr(text) }
        }
        process.terminationHandler = { task in
            output.fileHandleForReading.readabilityHandler = nil; errors.fileHandleForReading.readabilityHandler = nil
            // A fast machine operation can terminate between pipe delivery and
            // its readability callback. Drain both closed pipes before telling
            // the UI it is finished, otherwise a final job_completed JSONL
            // event (notably `inspect`) is intermittently lost.
            consumeOutput(output.fileHandleForReading.readDataToEndOfFile())
            let finalErrors = errors.fileHandleForReading.readDataToEndOfFile()
            if !finalErrors.isEmpty, let text = String(data: finalErrors, encoding: .utf8) { stderr(text) }
            finished(task.terminationStatus)
        }
        self.process = process
        try process.run()
    }
    public func cancel() { process?.interrupt() }
    public var isRunning: Bool { process?.isRunning == true }
}

private final class JSONLBuffer: @unchecked Sendable {
    private let lock = NSLock(); private var value = Data()
    func append(_ data: Data) -> [Data] {
        lock.lock(); defer { lock.unlock() }; value.append(data); var lines: [Data] = []
        while let newline = value.firstIndex(of: 10) { lines.append(value.prefix(upTo: newline)); value.removeSubrange(...newline) }
        return lines
    }
}
