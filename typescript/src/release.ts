import { createPublicKey, verify } from "node:crypto";

/** Litebackup's Ed25519 release keys by key ID, base64url without padding, as the litebackup command checks them. */
export const releaseKeys: Readonly<Record<string, string>> = {
  "01a1ead6-4b94-79ca-81f4-3a67c112bc0b": "t3C_3l71PimyhkPZOzJ_Xi4dOuHJ1CmSdDZ8lTm0vNI",
};

/** Frames signed release metadata: context, key ID, separator, document. */
const signatureContext = "clor-apps-v1\u0000";
const signatureSeparator = "\u0000";

/** One published file of a release. */
export interface ReleaseFile {
  name: string;
  filename: string;
  url: string;
  sha256: string;
  size: number;
  os?: string;
  architecture?: string;
}

/** A package release taken from its verified signed document. */
export interface Release {
  version: string;
  channels: string[];
  files: ReleaseFile[];
}

/** Options for findRelease. */
export interface FindReleaseOptions {
  /** Package slug; "cli" when unset. */
  package?: string;
  /** The release to find; the release on `channel` when unset. */
  version?: string;
  /** Used when `version` is unset; "stable" when unset. */
  channel?: string;
  /** Keys that verify the release; releaseKeys when unset. */
  keys?: Readonly<Record<string, string>>;
}

/** A release whose signature does not match Litebackup's release keys. */
export class SignatureError extends Error {
  constructor() {
    super("the release signature does not match Litebackup's release keys");
    this.name = "SignatureError";
  }
}

interface SignedDocument {
  key_id: string;
  document: string;
  signature: string;
}

interface PackageMetadata {
  packages: { slug: string; releases: { version: string; channels: string[]; signature: SignedDocument }[] }[];
}

/** Checks a signed release against keys and decodes it. */
function verifyRelease(signed: SignedDocument, keys: Readonly<Record<string, string>>): Omit<Release, "channels"> {
  const encoded = keys[signed?.key_id];
  if (!encoded) {
    throw new SignatureError();
  }
  let document: Buffer;
  let verified = false;
  try {
    const key = createPublicKey({ key: { kty: "OKP", crv: "Ed25519", x: encoded }, format: "jwk" });
    document = Buffer.from(signed.document, "base64url");
    const message = Buffer.concat([Buffer.from(signatureContext + signed.key_id + signatureSeparator), document]);
    verified = verify(null, message, key, Buffer.from(signed.signature, "base64url"));
  } catch {
    throw new SignatureError();
  }
  if (!verified) {
    throw new SignatureError();
  }
  try {
    const decoded = JSON.parse(document.toString("utf8")) as { version: string; artifacts: ReleaseFile[] };
    return { version: decoded.version, files: decoded.artifacts ?? [] };
  } catch {
    throw new SignatureError();
  }
}

/**
 * Reads package metadata, as https://litebackup.com/pkg/metadata serves
 * it, and returns the selected release taken from its signed document, never
 * the unsigned listing. Throws SignatureError when the signature does not verify.
 */
export function findRelease(metadata: string | unknown, options: FindReleaseOptions = {}): Release {
  const listing = (typeof metadata === "string" ? JSON.parse(metadata) : metadata) as PackageMetadata;
  const slug = options.package ?? "cli";
  const channel = options.channel ?? "stable";
  const keys = options.keys ?? releaseKeys;
  const listed = listing.packages?.find((candidate) => candidate.slug === slug);
  if (!listed) {
    throw new Error(`no package ${slug}`);
  }
  const candidate = listed.releases.find((release) => (options.version ? release.version === options.version : release.channels.includes(channel)));
  if (!candidate) {
    throw new Error(options.version ? `package ${slug} has no release ${options.version}` : `package ${slug} has no release on ${channel}`);
  }
  const release = verifyRelease(candidate.signature, keys);
  if (release.version !== candidate.version) {
    throw new SignatureError();
  }
  return { ...release, channels: candidate.channels };
}
