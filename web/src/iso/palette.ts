// Digital Twin colours (main screen). Orange = machinery accent; red-orange = alert only.
export const P = {
  bg0: "#0B1220",
  bg1: "#0E1A2B",
  floor: "#121C2E",
  floorEdge: "#3FA9F5",
  frame: "#FFFFFF",
  grid: "#1C2A42",
  panel: "rgba(11, 18, 32, 0.78)",
  cyan: "#3FA9F5",
  teal: "#2FD4A3",
  orange: "#F28C28",
  yellow: "#E8C547",
  amber: "#F5B83D",
  alert: "#FF4D2E",
  silver: "#B8C2CF",
  steel: "#7D8A9C",
  dark: "#26324A",
  pallet: "#3A2E22",
  cabin: "#E9EEF5",
  ink: "#E6EDF7",
  muted: "#7F8FA9",
} as const;

export function statusColour(status: string): string {
  if (status === "alarm" || status === "early_match") return P.alert;
  if (status === "watch") return P.amber;
  if (status === "learning" || status === "no_data") return P.muted;
  return P.teal;
}
