export const meta = {
  name: 'zen-bottleneck-research-remaining',
  description: 'Research where India depends on foreign supply, verify each claim adversarially, and turn it into filing-detection patterns',
  phases: [
    { title: 'Research', detail: 'one agent per theme: import dependence, policy, domestic build' },
    { title: 'Challenge', detail: 'adversarial check — real bottleneck, or already priced?' },
    { title: 'Patterns', detail: 'convert surviving themes into detectable filing language' },
  ],
}

const FRAME = `
CONTEXT

You are researching for Zen, an automated Indian-equity research platform. The
investor's thesis, in his own words:

  "India can come up in the next 3-5 years and catch up with markets like China
   in the AI/data centre space, nuclear tech, green energy, solar energy, water
   management and treatment, infra spending, defence spending. Bottlenecks are
   only there where India relies mainly on external suppliers."

That last sentence is the testable core: a BOTTLENECK is a point in a supply
chain where India currently depends on imports, and where domestic capacity is
being built. Companies building that capacity are the opportunity.

WHAT THIS IS FOR

The output is NOT a stock recommendation and must never be one. It is
sector-level intelligence that becomes a DETECTOR. The platform holds an
archive of NSE corporate announcements (the filings companies make to the
exchange). We want to spot, automatically and early, when a company announces
it is entering or expanding into one of these bottleneck areas.

A worked example of what we are trying to catch early: Sterlite Technologies
filed a press release on 16 June 2025 titled "STL expands Data Centre portfolio
to meet emerging requirements for AI data centres". The stock rose 19% that
session on 52x normal volume, went sideways for eight months, then re-rated
sevenfold. The filing was the detectable event.

RULES

1. Use web search and fetch real sources. Cite them. Prefer government data
   (Ministry of Commerce trade statistics, PIB releases, ministry reports),
   industry bodies, and recent credible reporting.
2. Be current. Import dependence changes fast, especially where a
   Production Linked Incentive (PLI) scheme has been running. A dependency
   that existed in 2020 may be substantially closed now. SAY SO if it has.
3. Quantify wherever possible: what share is imported, from where, what the
   domestic capacity target is, and by when.
4. NEVER name a stock as a buy, and never give a price view. Naming which
   listed companies operate in a segment is fine and useful; recommending
   them is not.
5. If a theme turns out NOT to be a real bottleneck -- because India is
   already self-sufficient, or because the constraint is demand rather than
   supply -- say so plainly. A negative finding is as valuable as a positive
   one and stops the investor chasing a story that has already played out.
`

const THEME_SCHEMA = {
  type: 'object',
  properties: {
    theme: { type: 'string' },
    is_real_bottleneck: { type: 'boolean' },
    import_dependence: { type: 'string', description: 'what share is imported, from where, with figures and dates' },
    specific_chokepoints: { type: 'array', items: { type: 'string' }, description: 'the precise components or materials India cannot yet make at scale' },
    policy_support: { type: 'string', description: 'PLI schemes, tariffs, mandates, budget allocations, with amounts and dates' },
    domestic_build: { type: 'string', description: 'what capacity is actually being built and by when' },
    already_priced: { type: 'string', description: 'honest read on whether this is a consensus trade already' },
    listed_exposure: { type: 'array', items: { type: 'string' }, description: 'listed Indian companies operating in this segment - descriptive only, NOT recommendations' },
    filing_language: { type: 'array', items: { type: 'string' }, description: 'exact phrases companies use in exchange filings when entering this space' },
    sources: { type: 'array', items: { type: 'string' } },
    confidence: { type: 'string', enum: ['high', 'medium', 'low'] },
  },
  required: ['theme', 'is_real_bottleneck', 'import_dependence', 'specific_chokepoints', 'filing_language', 'confidence'],
}

const CHALLENGE_SCHEMA = {
  type: 'object',
  properties: {
    theme: { type: 'string' },
    survives: { type: 'boolean' },
    strongest_objection: { type: 'string' },
    is_dependency_current: { type: 'boolean', description: 'is the import dependence still true today, or has it closed' },
    is_consensus: { type: 'boolean', description: 'is this already a crowded trade' },
    verdict: { type: 'string', enum: ['strong', 'moderate', 'weak', 'reject'] },
    evidence: { type: 'string' },
  },
  required: ['theme', 'survives', 'strongest_objection', 'verdict', 'evidence'],
}

// The seven the investor named, plus three classic India import-dependence
// stories he did not name but which sit upstream of several of his.
const THEMES = [
  { key: 'ai-datacentre', prompt: 'AI and DATA CENTRE infrastructure in India. Cover: power availability and cooling, GPUs and accelerators, network and optical fibre components, server and rack manufacturing, land and connectivity. Which parts are imported and which are being built domestically?' },
  { key: 'solar', prompt: 'SOLAR energy in India. Be specific about the full chain: polysilicon, ingots, wafers, cells, modules, and the silver paste used in cells. India has module capacity but the upstream is the question. Quantify what share of ingots and wafers is still imported from China and how far ALMM and PLI have closed it.' },
  { key: 'green-energy-storage', prompt: 'GREEN ENERGY beyond solar: wind, green hydrogen and electrolysers, and grid-scale battery storage. Which components are imported? What is the state of domestic electrolyser and battery cell manufacturing?' },
  { key: 'water', prompt: 'WATER management and treatment in India. Cover: membranes and reverse-osmosis elements, desalination, effluent treatment mandates, Jal Jeevan Mission and AMRUT spending. Is the constraint imported technology, or is it municipal funding and execution?' },
  { key: 'infrastructure', prompt: 'INFRASTRUCTURE spending in India. Cover: the national pipeline, roads, railways including high speed rail, ports, and the capital-goods supply chain. Be careful to distinguish a genuine supply bottleneck from ordinary cyclical spending, and say which this is.' },
  { key: 'defence', prompt: 'DEFENCE manufacturing and indigenisation in India. Cover: the positive indigenisation lists, offset rules, aero engines, avionics, propulsion, submarines, drones and counter-drone systems. Which subsystems still cannot be made domestically?' },
  { key: 'semiconductors', prompt: 'SEMICONDUCTORS and electronics manufacturing in India. Cover: fabs and assembly-test-packaging, the India Semiconductor Mission, display fabs, and PCB manufacturing. This sits upstream of several other themes, so be clear about what India can and cannot yet make.' },
  { key: 'critical-minerals', prompt: 'CRITICAL MINERALS and RARE EARTHS for India: lithium, cobalt, nickel, graphite, and rare-earth magnets. Cover import dependence on China, the National Critical Mineral Mission, and domestic processing capacity. Rare-earth magnets in particular are a known chokepoint for autos and defence.' },
  { key: 'power-grid', prompt: 'POWER TRANSMISSION and GRID equipment in India. Cover: transformers, HVDC, switchgear, conductors and cables, and grid-scale inverters. Note that data centres and renewables both depend on this, so it may be the binding constraint beneath two other themes.' },
]

phase('Research')
log(`Researching ${THEMES.length} themes, each independently challenged`)

const researched = await pipeline(
  THEMES,
  (t) => agent(`${FRAME}\n\nYOUR THEME\n${t.prompt}\n\nResearch this thoroughly using web search. Quantify import dependence with figures and dates.`, {
    label: `research:${t.key}`,
    phase: 'Research',
    schema: THEME_SCHEMA,
    effort: 'high',
  }),
  (res, original) => {
    if (!res) return null
    return agent(
      `${FRAME}\n\nYOU ARE THE SCEPTIC. Another researcher claims the following ` +
      `about ${res.theme}:\n\n` +
      `Real bottleneck: ${res.is_real_bottleneck}\n` +
      `Import dependence: ${res.import_dependence}\n` +
      `Chokepoints: ${(res.specific_chokepoints || []).join('; ')}\n` +
      `Already priced: ${res.already_priced || 'not addressed'}\n\n` +
      `Attack this with your own web research. Default to survives=false ` +
      `unless the evidence is strong. Specifically test:\n\n` +
      `1. Is the import dependence STILL TRUE, or has a PLI scheme or new ` +
      `domestic capacity already closed it? Cite current figures.\n` +
      `2. Is the constraint really SUPPLY, or is it demand, funding, land, or ` +
      `execution? Many "bottlenecks" are actually slow government payment.\n` +
      `3. Is this already a crowded consensus trade with the re-rating behind ` +
      `it rather than ahead of it?\n` +
      `4. Is there a structural reason India will NOT close this gap -- cost, ` +
      `scale economics, IP, or scarce inputs?\n\n` +
      `Be specific and cite sources. Vague scepticism is as useless as vague ` +
      `optimism.`,
      { label: `challenge:${original.key}`, phase: 'Challenge', schema: CHALLENGE_SCHEMA, effort: 'high' }
    ).then((c) => ({ research: res, challenge: c }))
  }
)

const clean = researched.filter(Boolean)
const survivors = clean.filter((r) => r.challenge && r.challenge.survives &&
  r.challenge.verdict !== 'reject')

log(`${clean.length} themes researched, ${survivors.length} survived the sceptic`)

phase('Patterns')
const detector = await agent(
  `${FRAME}\n\nYou are turning this research into a DETECTOR.\n\n` +
  `Here is every theme with its sceptical challenge:\n\n` +
  clean.map((r) => JSON.stringify({
    theme: r.research.theme,
    bottleneck: r.research.is_real_bottleneck,
    chokepoints: r.research.specific_chokepoints,
    filing_language: r.research.filing_language,
    verdict: r.challenge ? r.challenge.verdict : 'unknown',
    objection: r.challenge ? r.challenge.strongest_objection : '',
  }, null, 1)).join('\n\n') +
  `\n\nProduce a practical detection specification for scanning NSE corporate ` +
  `announcements. For each theme that survived, give:\n\n` +
  `- a short theme key (lowercase, underscores)\n` +
  `- 10-25 SPECIFIC regex-safe keyword phrases that would appear in a filing ` +
  `by a company entering or expanding in that space. Favour precise technical ` +
  `terms ("polysilicon", "rare earth magnet", "electrolyser", "HVDC", ` +
  `"liquid cooling") over generic ones ("growth", "expansion") which would ` +
  `match everything.\n` +
  `- phrases that would cause FALSE POSITIVES and should be excluded\n\n` +
  `Then write it as a Python module: a dict named THEMES mapping theme key to ` +
  `{"include": [...], "exclude": [...], "note": "..."}. Give the complete ` +
  `module text, ready to save as zen/monitor/themes.py, with a docstring ` +
  `explaining what it is and the caveat that a keyword match is a prompt to ` +
  `look, never a signal to buy.\n\n` +
  `Also state plainly which themes were REJECTED and why, so the investor ` +
  `knows what not to chase.`,
  { label: 'detector-spec', phase: 'Patterns', effort: 'high' }
)

return {
  researched: clean.length,
  survived: survivors.length,
  verdicts: clean.map((r) => ({
    theme: r.research.theme,
    bottleneck: r.research.is_real_bottleneck,
    verdict: r.challenge ? r.challenge.verdict : 'unknown',
    objection: r.challenge ? r.challenge.strongest_objection : '',
  })),
  detector,
}
