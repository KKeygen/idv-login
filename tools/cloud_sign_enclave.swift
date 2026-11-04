// Local, device-bound cloud-configuration signing key. No key is created unless
// the user explicitly invokes `generate`.
import Foundation
import Security
import LocalAuthentication
import Darwin

private let tag = Data("dev.idv-login.cloud-signing.v1".utf8)

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

private func keyQuery(context: LAContext? = nil) -> [CFString: Any] {
    var query: [CFString: Any] = [
        kSecClass: kSecClassKey,
        kSecAttrKeyType: kSecAttrKeyTypeECSECPrimeRandom,
        kSecAttrKeyClass: kSecAttrKeyClassPrivate,
        kSecAttrApplicationTag: tag,
        kSecAttrTokenID: kSecAttrTokenIDSecureEnclave,
        kSecReturnRef: true,
        kSecMatchLimit: kSecMatchLimitOne
    ]
    if let context { query[kSecUseAuthenticationContext] = context }
    return query
}

private func createKey() throws -> SecKey {
    var existingQuery = keyQuery()
    existingQuery.removeValue(forKey: kSecReturnRef)
    existingQuery[kSecReturnAttributes] = true
    let noInteraction = LAContext()
    noInteraction.interactionNotAllowed = true
    defer { noInteraction.invalidate() }
    existingQuery[kSecUseAuthenticationContext] = noInteraction
    var item: CFTypeRef?
    let existing = SecItemCopyMatching(existingQuery as CFDictionary, &item)
    guard existing == errSecItemNotFound else {
        if existing == errSecSuccess || existing == errSecInteractionNotAllowed || existing == errSecAuthFailed {
            throw SignerError.failure("signing key already exists or requires authentication; refusing replacement")
        }
        throw SignerError.failure("could not check existing key (OSStatus \(existing))")
    }
    var error: Unmanaged<CFError>?
    guard let access = SecAccessControlCreateWithFlags(nil,
        kSecAttrAccessibleWhenUnlockedThisDeviceOnly,
        [.privateKeyUsage, .biometryCurrentSet], &error) else {
        throw SignerError.failure("could not configure biometric key protection")
    }
    let attributes: [CFString: Any] = [
        kSecAttrKeyType: kSecAttrKeyTypeECSECPrimeRandom,
        kSecAttrKeySizeInBits: 256,
        kSecAttrTokenID: kSecAttrTokenIDSecureEnclave,
        kSecPrivateKeyAttrs: [
            kSecAttrIsPermanent: true,
            kSecAttrApplicationTag: tag,
            kSecAttrAccessControl: access
        ] as [CFString: Any]
    ]
    guard let key = SecKeyCreateRandomKey(attributes as CFDictionary, &error) else {
        throw SignerError.failure("Secure Enclave key generation failed: \(error?.takeRetainedValue().localizedDescription ?? "unknown error")")
    }
    return key
}

private func loadKey(context: LAContext) throws -> SecKey {
    var item: CFTypeRef?
    let status = SecItemCopyMatching(keyQuery(context: context) as CFDictionary, &item)
    guard status == errSecSuccess, let item, CFGetTypeID(item) == SecKeyGetTypeID() else {
        throw SignerError.failure("could not access Secure Enclave key (OSStatus \(status))")
    }
    // Type checked by CFGetTypeID above; SecKey is a CF opaque type.
    return (item as! SecKey)
}

private func publicPEM(_ key: SecKey) throws -> String {
    guard let publicKey = SecKeyCopyPublicKey(key) else {
        throw SignerError.failure("could not obtain public key")
    }
    var error: Unmanaged<CFError>?
    guard let raw = SecKeyCopyExternalRepresentation(publicKey, &error) as Data?,
          raw.count == 65, raw.first == 4 else {
        throw SignerError.failure("unexpected P-256 public key format")
    }
    // P-256 SubjectPublicKeyInfo header for a 65-byte X9.63 public point.
    let header: [UInt8] = [0x30,0x59,0x30,0x13,0x06,0x07,0x2a,0x86,0x48,0xce,0x3d,0x02,0x01,
                           0x06,0x08,0x2a,0x86,0x48,0xce,0x3d,0x03,0x01,0x07,0x03,0x42,0x00]
    let encoded = (Data(header) + raw).base64EncodedString()
    var lines: [String] = []
    var index = encoded.startIndex
    while index < encoded.endIndex {
        let end = encoded.index(index, offsetBy: 64, limitedBy: encoded.endIndex) ?? encoded.endIndex
        lines.append(String(encoded[index..<end]))
        index = end
    }
    return "-----BEGIN PUBLIC KEY-----\n" + lines.joined(separator: "\n") + "\n-----END PUBLIC KEY-----\n"
}

private func freshContext() -> LAContext {
    let context = LAContext()
    context.localizedReason = "确认签署 idv-login 云配置"
    // Do not request reuse of a previous Touch ID authorization. A fresh
    // context is used for the one private-key operation in this invocation.
    context.touchIDAuthenticationAllowableReuseDuration = 0
    return context
}

let args = CommandLine.arguments
let usage = "usage: cloud-sign-enclave generate | public | sign <cloudRes.json> <cloudRes.json.sig>"
guard args.count >= 2 else { fail(usage) }
do {
    switch args[1] {
    case "generate":
        guard args.count == 2 else { fail(usage) }
        let key = try createKey()
        print(try publicPEM(key), terminator: "")
    case "public":
        guard args.count == 2 else { fail(usage) }
        let context = freshContext()
        defer { context.invalidate() }
        print(try publicPEM(loadKey(context: context)), terminator: "")
    case "sign":
        guard args.count == 4 else { fail(usage) }
        let input = URL(fileURLWithPath: args[2])
        let output = URL(fileURLWithPath: args[3])
        guard input.standardizedFileURL != output.standardizedFileURL else {
            throw SignerError.failure("input and signature output must be different")
        }
        let data = try Data(contentsOf: input)
        let context = freshContext()
        defer { context.invalidate() }
        let key = try loadKey(context: context)
        let algorithm = SecKeyAlgorithm.ecdsaSignatureMessageX962SHA256
        guard SecKeyIsAlgorithmSupported(key, .sign, algorithm) else {
            throw SignerError.failure("key does not support P-256 SHA-256 signatures")
        }
        var error: Unmanaged<CFError>?
        guard let signature = SecKeyCreateSignature(key, algorithm, data as CFData, &error) as Data? else {
            throw SignerError.failure("signature failed: \(error?.takeRetainedValue().localizedDescription ?? "unknown error")")
        }
        let bytes = Data((signature.base64EncodedString() + "\n").utf8)
        try bytes.write(to: output, options: .atomic)
    default:
        fail(usage)
    }
} catch {
    fail(String(describing: error))
}
