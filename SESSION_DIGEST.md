# JBAiVideoSuite — Session Digest

## Stack & Environment
- **Frontend:** Next.js 16.3.4 (Pages Router, Turbopack) + React 19.2.8 + Tailwind v4 + Zustand, port 3000
- **Backend:** FastAPI + Prisma (`prisma-client-py`) + SQLite, port 8000
- **LM Studio:** 127.0.0.1:1234 (OpenAI-compatible)
- **ComfyUI:** 127.0.0.1:8188
- **DB:** `backend/prisma/database.db` (NOT `backend/dev.db` which is empty)
- **User runs services manually** — do NOT start `next dev`, `uvicorn`, `prisma generate`, or `prisma db push`
- PowerShell: use `;` not `&&`; npm needs `--cache` flag; Prisma CLI NOT in `node_modules`, `npx` blocked by sandbox
- DSH file policy: workspace-write; approval policy: ask

## Takes Feature (Complete)
- **Take model** (`schema.prisma`): `id` (cuid), `projectId`, `episode` (default 1), `endShotIndex` (Int), `prompt` (String, default ""), `videoPath` (String?), `createdAt`, `updatedAt`, `@@unique([projectId, episode, endShotIndex])`, `@@index([projectId, episode])`
- **Design decisions:** No `startShotIndex` (derived from prev take's `endShotIndex + 1`), no `order` field (ordered by `endShotIndex`), `id` is cuid, `prompt` editable in UI, consolidated actions/assets read-only, "Enable Takes" persists in localStorage (not DB), Delete Last Take only (no per-take delete, no insert), Generate Prompt button purple, Copy Prompt kept, total take duration sent to LLM (not per-shot), audio instructions per shot included in take prompt
- **LLM mapping:** `"Generate Take Video Prompt"` (separate from `"Generate Shot Video Prompt"` and `"Generate Image Prompt"`)
- **Delete pattern (all stages):** Immediate DELETE API call. If item has no `id` (unsaved), just remove from local state. No save needed.

## Backend Routes
- `backend/routes/takes.py` — Full CRUD:
  - `GET /projects/{id}/takes?episode=N` — list sorted by endShotIndex
  - `POST /projects/{id}/takes?episode=N` — upsert by endShotIndex, deletes takes not in payload, handles `videoPath`
  - `DELETE /projects/{id}/takes/{take_id}` — individual delete
  - `DELETE /projects/{id}/takes?episode=N` — delete all
  - `POST /projects/{id}/takes/generate-prompt?episode=N` — LLM call with: total take duration, per-scene sections (scene text + per-shot beats/camera/action/dialogue/audio instructions), consolidated reference assets (deduplicated), background music
- `backend/routes/shotlist.py` — `DELETE /{id}/shotlist/{shot_id}` added; POST deletes shots not in payload
- `backend/main.py` — `takes` router included

## Frontend Components
- `frontend/src/components/studio/TakesStage.tsx` (431 lines) — Take CRUD, consolidated states, generate prompt, save
- `frontend/src/components/studio/ShotListStage.tsx` (709 lines) — `takesEnabled`/`onTakesEnabledChange` props, "Enable Takes" checkbox, `id` field on ShotData, immediate DELETE
- `frontend/src/pages/index.tsx` — `TakesStage` import, `takesEnabled` state (localStorage), conditional tab array, dynamic tab labels, Takes tab section, passes `takesEnabled` to `FinalVideoStage`
- `frontend/src/components/studio/FinalVideoStage.tsx` (883 lines) — **Currently the largest code file**

## FinalVideoStage.tsx — Current State (883 lines)
- **Props:** `{ selectedEpisode: number; takesEnabled: boolean }`
- **State:** `subTab` ('generate'|'assemble'), `shots`, `selectedIdx`, `projectAssets`, `audioAssets`, `seed`, `videoUrl`, `isGenerating`, `isSaving`, `saved`, `assembleIdx`, `isPlaying`, `videoError`, `useTakes` (localStorage `finalVideoUseTakes`, default false), `takes`, `selectedTakeIdx`
- **Refs:** `selectedIdxRef`, `selectedTakeIdxRef`, `useTakesRef` (for pollStatus)
- **Key effects:**
  - `useEffect` reads localStorage `finalVideoUseTakes` on mount
  - `useEffect` resets `assembleIdx` to 0 when `useTakes` changes
  - `useEffect` forces `useTakes=false` when `takesEnabled` is false
  - Fetches shots, assets, audio, and takes (if `takesEnabled`) on mount/episode change
- **Take computations:** `getTakeStartShot(takeIdx)`, `getTakeShots(takeIdx)`, `takeShots`, `takeDuration`, `takeLocationStates`, `takeCharacterStates`, `takePropStates`, `takeAudios` (all deduplicated)
- **Generate tab:**
  - Shot selector (prev/next/dropdown) when `!useTakes`
  - Take selector (prev/next/dropdown) when `useTakes`
  - Video preview (uses `selectedTake.videoPath` or `shot.videoPath`)
  - Shot detail pane (prompt, assets, audio, duration, seed, gen/save) when `!useTakes`
  - Take detail pane (prompt 12-row, total duration with >15s warning, consolidated assets, audio, seed, gen/save) when `useTakes`
  - `handleGenerate` picks prompt/shotNum based on `useTakes`
  - `handleSave` POSTs to `/takes` or `/shotlist` based on `useTakes`
  - `pollStatus` updates `videoPath` on take (if `useTakesRef.current` && resolution='high') or shot
- **Assemble tab:**
  - Condition: `(useTakes ? takes.length > 0 : shots.length > 0)` — renders video preview, play sequence, strip for BOTH modes
  - Video preview: `takes[assembleIdx].videoPath` or `shots[assembleIdx].videoPath`
  - Play sequence: iterates `useTakes ? takes : shots`
  - Strip: take thumbnails ("Take N") or shot thumbnails (shot number)
  - "No takes found" message only when `useTakes && takes.length === 0`
  - Export button (disabled)
- **Source radio:** On sub-tab row, right-aligned, visible in both tabs, only when `takesEnabled`. Options: Use Shots (default) / Use Takes. Persists via localStorage.

## Solved Bugs
- **SSR `localStorage` ReferenceError:** Guard with `useEffect`
- **Deleted takes/shots reappearing:** POST endpoints now delete items not in payload
- **Delete required save:** All deletes are immediate API calls
- **"No takes found" showing when takes disabled:** `useEffect` forces `useTakes=false` when `takesEnabled` is false
- **Assemble tab empty for shots:** Condition was `{useTakes && takes.length > 0}` which hid everything in shots mode. Fixed to `{(useTakes ? takes.length > 0 : shots.length > 0)}`
- **`assembleIdx` out of bounds on source switch:** Reset to 0 in `useEffect` on `useTakes` change

## Verified Working Flows
- Takes CRUD (add, delete last, save)
- Take prompt generation via LLM
- Final Video Generate tab: shots mode and takes mode (selector, detail pane, gen low/high, save)
- Final Video Assemble tab: shots mode and takes mode (preview, play sequence, strip)
- Source radio persistence via localStorage
- `videoPath` on takes updated via `pollStatus` on high-res completion

## Pending Tasks
1. **User needs to run `prisma db push` + `prisma generate`** to add `videoPath` column to Take table
2. **User needs to create `prompts/GenerateTakeVideoPrompt_SystemPrompt.txt`** with the system prompt content
3. **Push to origin** — branch `main`, 2 commits ahead of `origin/main` (latest: `7d4f2b8`)
4. **"Edit shot & save page resets" bug** — user deferred ("will get back to this later"). Root cause: `loadFromRaw` (line 141, called at line 681 "Load Shots" button) calls `setShots(parsed)` + `setSelectedIdx(0)`
5. **Shot duration clustering at 3.75s (Rule 19)** — user aware, no change requested

## Critical Context
- **Non-regression & scope lockdown:** Never refactor/rename/rewrite working routes, imports, or interfaces unless explicitly instructed
- **LLM auto-save audit:** Only two auto-savers: Shot Generate All and Asset Generate All. All other LLM call sites stay in local frontend state until explicit save
- **Resolution data migration:** If saved `resolutionJson` has old `{low: {...}, high: {...}}` shape, user needs to re-save in Settings
- **`generate-prompt` in `assets.py` and `AssetsStage.tsx` was REVERTED** — do NOT re-add `characteristics` to the prompt payload
- **`chat.py` logging was removed** — do NOT re-add
- **M9, M13, M16, M19, M30, M37, M40, M43, M44, M46–M51: SKIP** — do not revisit
- **`patch_db.py`** is at repo root (not in backend/) — run from backend dir: `python ..\patch_db.py`
- **Last commit:** `7d4f2b8` "Add takes support to Final Video stage and update docs" (5 files)
- **Unstaged:** `assets/workflows/MiniMax_H3_R2V_9Img3Aud_Upscale_Omen.json` (separate change, not takes-related)
- **Untracked:** `backend/dev.db` (empty, ignore)

### Operational Directives

#### 1. Modular Architecture
- Keep components self-contained. `FinalVideoStage.tsx` is already 883 lines — if it grows further, consider extracting take/shot detail panes into sub-components, but only if explicitly requested.
- Backend routes are per-domain (`takes.py`, `shotlist.py`, `assets.py`). Follow this pattern for any new domain.

#### 2. Non-Regression & Scope Lockdown
- Never refactor, rename, or rewrite working routes, imports, or interfaces unless explicitly instructed.
- Before making file changes, explicitly state:
  1. The target file path and current size.
  2. The plan (whether creating a companion module or editing in place).
  3. Confirmation that existing working flows will remain intact.
  4. Never remove any added logs without explicitly asking for it.

#### 3. Ask for User Assistance
- If you don't have internet search ability, ask the user to do it for you.
- If you need assistance with Prisma DB updates (`prisma generate`, `prisma db push`), ask the user to run them.
- Do NOT attempt to start `next dev`, `uvicorn`, or any service. The user runs all services manually.
