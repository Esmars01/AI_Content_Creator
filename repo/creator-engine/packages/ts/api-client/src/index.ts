// Generated types for the domain models (VideoSpec, CBS, DNA, memory, coverage) and the API.
// Regenerate with `make gen-schema` (models.ts) and `make gen-client` (api.ts); never edit them by hand.
export type { components as Models } from "./models";
export type { components as Api, operations as ApiOperations, paths as ApiPaths } from "./api";

import type { components } from "./models";

export type VideoSpec = components["schemas"]["VideoSpec"];
export type CanonicalBehaviorSpec = components["schemas"]["CanonicalBehaviorSpec"];
export type BehaviorCoverageReport = components["schemas"]["BehaviorCoverageReport"];
export type CreatorDNA = components["schemas"]["CreatorDNA"];
export type WorldDNA = components["schemas"]["WorldDNA"];
export type MemoryItem = components["schemas"]["MemoryItem"];
export type EditOperations = components["schemas"]["EditOperations"];
export type EditOperation = components["schemas"]["EditOperations"]["operations"][number];
export type SpecPatch = components["schemas"]["SpecPatch"];
