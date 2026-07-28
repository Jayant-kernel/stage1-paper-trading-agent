import { createPublicKey, verify } from "node:crypto";
import { lstatSync, readFileSync } from "node:fs";
import {
  CARD_RELATIVE,
  PUBLIC_KEY_RELATIVE,
  SIGNATURE_RELATIVE,
  fixedExistingFile,
  publicFingerprint,
  readCanonicalCard,
  strictSignatureBytes,
  testPaths,
  verifyRepositoryAssertions,
} from "./change_card_common.mjs";

function verifyFiles(cardPath, signaturePath, publicPath, assertions) {
  for (const path of [cardPath, signaturePath, publicPath]) {
    const status = lstatSync(path);
    if (!status.isFile() || status.isSymbolicLink()) throw new Error("INPUT_NOT_REGULAR");
  }
  const { card, bytes } = readCanonicalCard(cardPath);
  if (assertions) verifyRepositoryAssertions(card);
  const signatureText = readFileSync(signaturePath, "utf8");
  const signature = strictSignatureBytes(signatureText);
  try {
    const publicPem = readFileSync(publicPath, "utf8");
    const publicKey = createPublicKey(publicPem);
    if (publicKey.asymmetricKeyType !== "ed25519") {
      throw new Error("PUBLIC_KEY_NOT_ED25519");
    }
    if (!verify(null, bytes, publicKey, signature)) {
      throw new Error("SIGNATURE_INVALID");
    }
    process.stdout.write(JSON.stringify({
      fingerprint: publicFingerprint(publicPem),
      status: "VERIFIED",
    }));
  } finally {
    signature.fill(0);
  }
}

const [mode, argument] = process.argv.slice(2);
if (mode === "--verify" && argument === undefined) {
  verifyFiles(
    fixedExistingFile(CARD_RELATIVE),
    fixedExistingFile(SIGNATURE_RELATIVE),
    fixedExistingFile(PUBLIC_KEY_RELATIVE),
    true,
  );
} else if (mode === "--verify-test" && argument) {
  const paths = testPaths(argument, { requireExisting: true });
  verifyFiles(paths.card, paths.signature, paths.publicKey, false);
} else {
  throw new Error("USAGE_ERROR");
}
