// Markdown rendering for copilot prose.
//
// V1 used `react-markdown`, which renders to React elements and therefore never
// produces an HTML string. Vue's equivalent is `v-html`, which does — so the
// renderer has to be the thing that refuses raw HTML, because the sink no longer
// can. `html: false` makes markdown-it escape any `<script>`/`<img onerror>` an
// LLM reply might contain instead of passing it through, which is the same
// guarantee react-markdown gave by construction.
//
// This is the one place in the app allowed to feed `v-html`.
import MarkdownIt from 'markdown-it'

const md = new MarkdownIt({
  html: false, // load-bearing: see above
  linkify: true,
  breaks: true, // a single newline in a chat reply is meant as a line break
})

/** Render Markdown to HTML with raw HTML escaped. Safe for `v-html`. */
export function renderMarkdown(source: string): string {
  return md.render(source ?? '')
}
