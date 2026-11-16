/** Soft affinity choices for server preferred_regions (matches runner PORTUNUS_REGIONS). */

export type RoutingChoice = "any" | "domestic" | "overseas";

export const ROUTING_CHOICES: { id: RoutingChoice; label: string }[] = [
  { id: "any", label: "Any" },
  { id: "domestic", label: "Domestic" },
  { id: "overseas", label: "Overseas" },
];

/**
 * Map stored preferred_regions to a segment choice.
 * Returns null when tags are neither empty nor a single domestic/overseas preference
 * (unknown custom tags or mixed lists) so the UI does not misreport them as Any.
 */
export function regionsToChoice(regions: string[] | null | undefined): RoutingChoice | null {
  const list = regions ?? [];
  if (list.length === 0) return "any";
  if (list.length === 1 && list[0] === "domestic") return "domestic";
  if (list.length === 1 && list[0] === "overseas") return "overseas";
  return null;
}

export function choiceToRegions(choice: RoutingChoice): string[] {
  if (choice === "domestic") return ["domestic"];
  if (choice === "overseas") return ["overseas"];
  return [];
}
