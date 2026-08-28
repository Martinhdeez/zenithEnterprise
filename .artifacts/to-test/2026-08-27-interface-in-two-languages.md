# The interface in English and in Spanish

**Status:** implemented, `make check` green, not yet exercised in front of an audience.

The demonstration is given in Spanish, to a Spanish buyer, over a corpus that is 59.4%
Spanish by passage — and every button said `Upload`. The answers were already Spanish:
prompt rule 11 fixes the answer to the language of the question. Only the chrome was English.

---

## The decision

**One build with a switch, not a build per language.** A branch per language means two
divergent copies of forty-four files, and it cannot do the thing that actually sells: change
the language in the room, in front of them, on the same screen.

**No library.** For two languages `react-i18next` brings a dependency that has to clear the
licence gate in `make check`, a provider, a hook and a loader, to replace what fits in sixty
lines. `Intl.PluralRules` is already in the browser and is the only part genuinely hard to get
right by hand.

**Keys are the English sentences.** `t("Search your corpus")` reads at the call site as what
it renders; a dotted token needs a second file open to review. There is therefore no English
catalogue to fall out of date, and no key that can render as a blank space.

---

## Shape

```
shared/i18n/
  index.ts    translate(), the Intl formatters, EN_PLURALS
  useT.ts     useT() and useFormat(), over useSyncExternalStore
  es.ts       the Spanish catalogue
  es.test.ts  the four guards
```

The language is an external store rather than a React context: a context needs a provider
wrapping the tree and re-renders everything under it, which is correct but heavy for a value
that changes twice in a demonstration.

Register is **"tú", never "usted"** — the product is a tool somebody uses all day, not a form
they fill in for an institution. Translation is by sense, not literal: "found nothing" is
"sin resultados", not "encontró nada".

---

## What is deliberately not translated

- **`ABSTENTION` in `citations.py`**, compared as a string to decide that the model refused.
  Translating it breaks invariant 5. It never reaches the screen anyway — the UI renders its
  own banner, which is translated.
- **Server messages.** Forty-seven places show `failure.message` verbatim and the backend
  holds ~138 user-facing strings. Doing those means `Accept-Language` and the exception layer,
  which did not fit before the demonstration. **This is the one visible seam left:** if
  something fails on screen, the message appears in English.
- **Document, label and organisation names** — the customer's own data — and identifiers a
  reader would search for verbatim ("Form 941", article numbers, model names). The generation
  prompt already applies the same rule to answers, so the interface and the model agree about
  what stays in the original.

---

## The failure mode this feature has, and the tests for it

A green catalogue proves every `t(…)` resolves. **It proves nothing about a string that never
became a `t(…)`** — and that is where every defect actually found lived. Four guards, in
`es.test.ts`:

1. **Every key the interface asks for has a Spanish sentence.** Reads the source rather than
   comparing catalogues, because with English keys there is no English catalogue to compare
   against. Its reader is a scanner rather than a regular expression: `t("(optional)")` has a
   closing parenthesis inside the key, and any pattern ending the call at the first `)` reports
   a live translation as unused.
2. **No entry that nothing asks for.** A dead translation is the one that stays when the
   English changes, so the next person reads it as current and translates around it.
3. **Every user-visible attribute is wrapped in `t()`.** `label=`, `placeholder=`,
   `aria-label=`, `title=`, `hint=`, `empty=`, `headline=`, `alt=`. This is the guard for the
   class above, and it was added after a manual walk found what the other three could not see.
4. **Plurals count correctly in English too.** See below.

### Four seams the tests could not see, found by walking the screens

- **`lang` on the document was set only by the control**, so a reload of a Spanish
  installation rendered Spanish text under `lang="en"` — invisible on screen and wrong in the
  one place it matters, since it is what a screen reader uses to choose a voice. It now comes
  from the same inline block in `index.html` that applies the theme before first paint.
- **Two sort tables held their labels as module constants.** `SORTS` was evaluated once at
  import, before anyone had chosen a language, so three options were frozen in English for the
  whole session. Built per render now.
- **Whole screens whose labels arrived as props.** `Profile`, `DocumentDetail`, `Analytics`,
  `SetPassword`, `AccessMatrix`, `SingleUseLink`. Invisible to a reviewer reading a diff of the
  catalogue, and half of them are read aloud rather than drawn.
- **Dates and numbers followed the browser, not the application.** Fifteen call sites used
  `toLocaleDateString()` with no locale, which resolves to `navigator.language` — the machine's
  setting. The switch moved the words and left `8,273` and the date order reading in whichever
  language the laptop happened to be set to. `useFormat()` binds them to the choice; `en-GB`
  rather than `en-US` because this is deployed in Europe over Spanish and EU law.

### The bug the source language hides

English keys are their own translation, which works for every string except a plural: a key
cannot be both "1 answer" and "3 answers". `{count} answer` rendered **"3 answer"** on an
English screen — the Spanish was right and the source language was not, which is the one
direction nobody thinks to check. `EN_PLURALS` carries the few keys that need it, and should
stay small: most sentences can be written so the count sits apart from the noun.

---

## Verification

- `make check` green: backend suite, frontend suite, `pyright`, `tsc`, licences.
- 331 catalogue entries against 409 `t()` call sites across the screens.
- An independent sweep for visible attributes and loose JSX text outside `t()` returns zero.

## What would tell us this is wrong

Somebody walking the product in Spanish and finding an English word. Every defect above was
found that way and none of them by the suite, so the suite is the floor here, not the ceiling.
