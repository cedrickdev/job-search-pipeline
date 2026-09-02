import { scoreTone } from "../lib/status";
import "./ScoreChip.css";

export function ScoreChip({ score }: { score: number | null }) {
  const tone = scoreTone(score);
  return <span className={`score-chip ${tone}`}>{score === null ? "—" : score}</span>;
}
