// Types for search.mjs (the demo page's retrieval), for the TypeScript test.
export interface Passage {
  id: string;
  source: string;
  title: string;
  text: string;
  start?: number;
  scene?: number;
}
export interface Indexed extends Passage {
  len: number;
  tf: Record<string, number>;
}
export interface Index {
  version: number;
  k1: number;
  b: number;
  avg: number;
  idf: Record<string, number>;
  docs: Indexed[];
}
export interface Result {
  id: string;
  source: string;
  title: string;
  text: string;
  start: number | null;
  score: number;
  coverage: number;
}
export const K1: number;
export const B: number;
export const DECLINE_BELOW: number;
export const TOP: number;
export function tokens(text: string): string[];
export function buildIndex(passages: Passage[]): Index;
export function unseenIdf(index: Index): number;
export function coverage(index: Index, doc: Indexed, query: string[]): number;
export function search(
  index: Index,
  question: string,
  top?: number,
): { declined: boolean; results: Result[]; chapters: number[] };
