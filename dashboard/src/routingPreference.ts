/** Soft affinity choices for server preferred_regions (matches runner PORTUNUS_REGIONS). */

export type RoutingChoice = "any" | "domestic" | "overseas";

export const ROUTING_CHOICES: { id: RoutingChoice; label: string }[] = [
  { id: "any", label: "Any" },
  { id: "domestic", label: "Domestic" },
  { id: "overseas", label: "Overseas" },
];

export function regionsToChoice(regions: string[] | null | undefined): RoutingChoice {
  const list = regions ?? [];
  if (list.length === 1 && list[0] === "domestic") return "domestic";
  if (list.length === 1 && list[0] === "overseas") return "overseas";
  if (list.includes("overseas") && !list.includes("domestic")) return "overseas";
  if (list.includes("domestic") && !list.includes("overseas")) return "domestic";
  return "any";
}

export function choiceToRegions(choice: RoutingChoice): string[] {
  if (choice === "domestic") return ["domestic"];
  if (choice === "overseas") return ["overseas"];
  return [];
}
