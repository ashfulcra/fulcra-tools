// Presentation only. No schema, ownership, freshness or completed-setup proof.
import { UUID } from "./v1AuthStorage";
import { snapshotV1JSON } from "./v1Http";
import type { V1OwnedType } from "./v1OwnedTypes";

export type V1DestinationCandidate = Readonly<Pick<V1OwnedType, "id" | "name">>;
export interface V1DestinationCandidates {
  readonly version: 1;
  readonly status: "candidates";
  readonly accountId: string;
  readonly candidates: readonly V1DestinationCandidate[];
}

function exact(value: unknown, keys: string[]): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value)
    && Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key));
}

/** Validate a future candidate-only page response against current display data.
 * This account comparison is UI scoping, never principal or setup authority.
 * Closed fields prevent credentials/schema/provenance from entering this DTO.
 */
export function projectV1DestinationCandidates(value: unknown, currentAccount: unknown): readonly V1DestinationCandidate[] {
  try {
    const response = snapshotV1JSON(value, 64 * 1024);
    if (typeof currentAccount !== "string" || !UUID.test(currentAccount)
        || !exact(response, ["version", "status", "accountId", "candidates"])
        || response.version !== 1 || response.status !== "candidates" || response.accountId !== currentAccount
        || !Array.isArray(response.candidates) || response.candidates.length > 128) throw new Error();
    const seen = new Set<string>();
    const rows = response.candidates.map(row => {
      if (!exact(row, ["id", "name"]) || typeof row.id !== "string"
          || !row.id.startsWith("Event/") || !UUID.test(row.id.slice(6)) || seen.has(row.id)
          || typeof row.name !== "string" || !row.name.trim() || row.name.length > 256
          || /[\x00-\x1f\x7f]/.test(row.name)) throw new Error();
      seen.add(row.id);
      return Object.freeze({ id: row.id, name: row.name });
    });
    return Object.freeze(rows);
  } catch {
    throw new Error("Candidate list unavailable. Setup remains held.");
  }
}
