// Local, device-bound cloud signing. `generate` is the only key-creation path.
import Foundation
import Security
import LocalAuthentication
import CryptoKit
import Darwin

private enum SignerError: Error, CustomStringConvertible {
    case failure(String)
    var description: String {
        switch self { case .failure(let message): return message }
    }
}

private func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data(("cloud signer: " + message + "\n").utf8))
    exit(1)
}

private func freshContext() -> LAContext {
    let context = LAContext()
    context.localizedReason = "确认签署 idv-login 云配置"
    context.touchIDAuthenticationAllowableReuseDuration = 0
    return context
}

// Anchor file operations to an opened directory descriptor.  Reject symlinks in
// the directory path and use O_NOFOLLOW/O_EXCL for the final component.
private func parentDirectory(_ path: String) throws -> (Int32, String) {
    let url = URL(fileURLWithPath: path).standardizedFileURL
    let directory = url.deletingLastPathComponent()
    guard directory.path == directory.resolvingSymlinksInPath().path else {
        throw SignerError.failure("key or output directory contains a symbolic link")
    }
    let name = url.lastPathComponent
    guard !name.isEmpty && name != "." && name != ".." else {
        throw SignerError.failure("invalid file name")
    }
    let fd = open(directory.path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW)
    guard fd >= 0 else { throw SignerError.failure("could not open parent directory (errno \(errno))") }
    return (fd, name)
}

private func writeNewFile(_ data: Data, at path: String) throws {
    let (dir, name) = try parentDirectory(path)
    defer { close(dir) }
    let temp = ".idv-cloud-sign-" + UUID().uuidString + ".tmp"
    let fd = openat(dir, temp, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, mode_t(0o600))
    guard fd >= 0 else { throw SignerError.failure("could not create temporary file (errno \(errno))") }
    defer { close(fd); unlinkat(dir, temp, 0) }
    try data.withUnsafeBytes { bytes in
        var offset = 0
        while offset < bytes.count {
            let n = write(fd, bytes.baseAddress!.advanced(by: offset), bytes.count - offset)
            if n < 0 && errno == EINTR { continue }
            guard n > 0 else { throw SignerError.failure("could not write temporary file (errno \(errno))") }
            offset += n
        }
    }
    guard fsync(fd) == 0 else { throw SignerError.failure("could not sync temporary file (errno \(errno))") }
    // linkat fails if the final name already exists, including an existing symlink.
    guard linkat(dir, temp, dir, name, 0) == 0 else {
        throw SignerError.failure("refusing to replace existing file or link (errno \(errno))")
    }
    _ = fsync(dir)
}

private func requireAbsent(_ path: String) throws {
    let (dir, name) = try parentDirectory(path)
    defer { close(dir) }
    var info = stat()
    if fstatat(dir, name, &info, AT_SYMLINK_NOFOLLOW) == 0 {
        throw SignerError.failure("refusing to replace existing file or link")
    }
    guard errno == ENOENT else { throw SignerError.failure("could not inspect key path (errno \(errno))") }
}

private func readHandle(_ path: String) throws -> Data {
    let (dir, name) = try parentDirectory(path)
    defer { close(dir) }
    let fd = openat(dir, name, O_RDONLY | O_NOFOLLOW)
    guard fd >= 0 else { throw SignerError.failure("could not open key handle (errno \(errno))") }
    defer { close(fd) }
    var info = stat()
    guard fstat(fd, &info) == 0,
          (info.st_mode & mode_t(S_IFMT)) == mode_t(S_IFREG),
          info.st_uid == getuid(), (info.st_mode & mode_t(0o077)) == 0,
          info.st_nlink == 1, info.st_size > 0, info.st_size <= 16_384 else {
        throw SignerError.failure("key handle must be an owner-only regular file with one link")
    }
    var data = Data(count: Int(info.st_size))
    let count = try data.withUnsafeMutableBytes { bytes -> Int in
        var offset = 0
        while offset < bytes.count {
            let n = read(fd, bytes.baseAddress!.advanced(by: offset), bytes.count - offset)
            if n < 0 && errno == EINTR { continue }
            guard n > 0 else { throw SignerError.failure("could not read complete key handle") }
            offset += n
        }
        return offset
    }
    guard count == data.count else { throw SignerError.failure("incomplete key handle") }
    return data
}

private func publicPEM(_ key: SecureEnclave.P256.Signing.PrivateKey) -> String {
    let encoded = key.publicKey.derRepresentation.base64EncodedString()
    var lines: [String] = []
    var index = encoded.startIndex
    while index < encoded.endIndex {
        let end = encoded.index(index, offsetBy: 64, limitedBy: encoded.endIndex) ?? encoded.endIndex
        lines.append(String(encoded[index..<end]))
        index = end
    }
    return "-----BEGIN PUBLIC KEY-----\n" + lines.joined(separator: "\n") + "\n-----END PUBLIC KEY-----\n"
}

let args = CommandLine.arguments
let usage = "usage: cloud-sign-enclave generate <key-handle> | public <key-handle> | sign <key-handle> <payload> <signature>"
guard args.count >= 2 else { fail(usage) }
do {
    guard SecureEnclave.isAvailable else { throw SignerError.failure("Secure Enclave unavailable") }
    switch args[1] {
    case "generate":
        guard args.count == 3 else { fail(usage) }
        try requireAbsent(args[2])
        var error: Unmanaged<CFError>?
        guard let access = SecAccessControlCreateWithFlags(nil, kSecAttrAccessibleWhenUnlockedThisDeviceOnly,
                [.privateKeyUsage, .biometryCurrentSet], &error) else {
            throw SignerError.failure("could not configure biometric key protection")
        }
        let key = try SecureEnclave.P256.Signing.PrivateKey(accessControl: access)
        try writeNewFile(key.dataRepresentation, at: args[2])
        print(publicPEM(key), terminator: "")
    case "public":
        guard args.count == 3 else { fail(usage) }
        let context = freshContext()
        defer { context.invalidate() }
        let key = try SecureEnclave.P256.Signing.PrivateKey(dataRepresentation: readHandle(args[2]), authenticationContext: context)
        print(publicPEM(key), terminator: "")
    case "sign":
        guard args.count == 5 else { fail(usage) }
        guard URL(fileURLWithPath: args[2]).standardizedFileURL != URL(fileURLWithPath: args[4]).standardizedFileURL,
              URL(fileURLWithPath: args[3]).standardizedFileURL != URL(fileURLWithPath: args[4]).standardizedFileURL else {
            throw SignerError.failure("output must differ from key handle and payload")
        }
        let data = try Data(contentsOf: URL(fileURLWithPath: args[3]))
        let context = freshContext()
        defer { context.invalidate() }
        let key = try SecureEnclave.P256.Signing.PrivateKey(dataRepresentation: readHandle(args[2]), authenticationContext: context)
        let signature = try key.signature(for: data).derRepresentation
        try writeNewFile(Data((signature.base64EncodedString() + "\n").utf8), at: args[4])
    default:
        fail(usage)
    }
} catch {
    fail(String(describing: error))
}
