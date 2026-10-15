# Left navigation and palette QA — 13 October 2026

final result: passed

## Findings

- No actionable P0/P1/P2 findings remain within the requested left-navigation and palette scope. The user reattached the source image; the combined full-view and focused comparisons were created and opened before this result was changed to passed.

## Scope and evidence

Historical source visual: the user's supplied CRM screenshot. A private working copy was retained outside this repository to prevent temporary-file expiry. It is not included in GitHub or imported into ERP data. Original-machine evidence paths have been omitted from this portable handover.

Source dimensions: 3826×2152 pixels, corresponding to 1913×1076 CSS pixels at 2× density. The embedded ICC profile was converted to sRGB and downsampled to 1913×1076 before comparison. The fresh native browser capture is exactly 1913×1076 CSS/image pixels with devicePixelRatio 1. A separate fresh preview is 1280×720 at devicePixelRatio 1. An earlier browser tab's 130% zoom was not used as the comparison capture; its zoom preference was not changed.

Implementation: `http://127.0.0.1:8810/odoo/action-604`.

Implementation screenshot: `../jakh-erp-sidebar-preview.jpg` (1280×720). Additional private native browser captures are in the workspace's `work/sidebar-qa/`: `desktop-reference-size.jpg` (1913×1076), `desktop-collapsed.jpg` (1280×720), `tablet-dashboard.jpg` (820×1180), `mobile-dashboard.jpg` and `mobile-drawer.jpg` (390×844). Captures use CSS-size image output; exact dimensions confirmed. The source reference includes actual CRM opportunities; the ERP is a different route with an empty synthetic operational dashboard. The requested scope is the left-column navigation and palette, not a clone of CRM content or import of screenshot data.

Historical full-view comparison: 3826×1120, containing the normalized source and implementation together. Focused sidebar/header comparison: 1040×680, using equal-size 520×636 crops from both images. Those private comparison images were opened and inspected on the original host; they are not included here or reproduced evidence for a new host. The source CRM populated screen and ERP synthetic dashboard have intentionally different routes and data; visual judgment is limited to the user's requested navigation/palette transfer.

## Implementation checks completed

- Native menu tree, server-filtered permissions and native action service retained. No access groups, financial validation or customer data changed by this interface update.
- Construction ERP and Quality/HSE workspace menus opened. Shared Dashboard, Reports, Approvals and Administration derive from the native permitted menu set.
- Reports expanded and opened the trial-balance wizard. Quality/HSE opened list and pivot views. Temporary report and estimate forms were discarded through native controls.
- An incomplete new estimate with a temporary title could not navigate away: native missing-required-fields protection kept the form open. Changing the workspace selector did not discard it. No estimate was saved by verification.
- Desktop rail changes the layout offset from 224px to 70px; expansion restores it. Tablet layout has two KPI columns; phone has one. No document-level horizontal overflow at tested widths.
- Stored desktop collapse does not hide phone submenu children. Phone drawer traps Shift+Tab at its first control, Escape restores focus to the opener, and successful native action navigation closes it. Hidden drawer is not reachable through its native inert/hidden state.
- White utility header, navy sidebar, teal actions, pale background and white panels render successfully. Standard bundled icon library used; missing Dashboard glyph corrected to the supported native app-grid icon.
- Current browser error and warning log checked: none recorded on final preview tab.
- 25 native tagged workflow/branding tests passed per isolated customer; 18 structural/deployment, ten estimating and six operations arithmetic tests passed.

## Required fidelity surfaces

- Fonts/typography: the focused comparison shows the same system-sans character and compact text hierarchy. Native system font retained, 32px desktop heading, compact 12–13px menu text; long ERP labels wrap rather than clip. Weight, line-height and letter spacing remain readable at all tested widths. The source's CRM headings/content are not being reproduced as ERP data.
- Spacing/layout rhythm: the focused comparison preserves the dark vertical navigation, inset selected row, left accent, restrained borders and white rounded content surfaces. The 224px sidebar intentionally accommodates longer ERP labels (reference approximately 204px); the workspace selector and existing 48px utility header are retained for the two-workspace ERP. These alter vertical positions intentionally, not as an exact CRM-page clone. Dashboard grid differs because it shows real operational measures rather than the source sales KPIs.
- Colors/tokens: sRGB source and native UI use sidebar `#172e39`, selected `#31514f`, teal `#146e65`, canvas `#f6f8f7`, white `#ffffff`, ink `#20333b`, border `#e0e7e6`. Computed sidebar/selected/canvas colors confirmed and the combined comparison shows matching color families and foreground/background balance. Native semantic status colors are retained; no silent recoloring of business status.
- Image quality/assets: no decorative raster assets required in this scope. Existing JAKH identity and actual user/customer identity remain; standard bundled icons are sharp and consistently aligned. The reference's CRM monogram is not substituted with an invented ERP graphic; logo redesign was not requested. No sample CRM data, placeholder avatars, decorative CSS art or new handcrafted illustrations introduced.
- Copy/content: existing ERP labels retained; dashboard counts remain real accessible-record counts, explicitly synthetic here. No fake financial totals or wording suggesting statutory acceptance.

## Repairs and interaction history

- Native stylesheet compiler rejected a CSS `min()` expression: replaced with width/max-width; rebuilt both databases and verified live styles.
- Initial action could highlight an invisible app root: selection now prioritizes rendered Dashboard/leaf menus.
- Persisted collapsed preference could hide phone children: collapsed class suppressed on mobile.
- Phone brand title/subtitle became inline: explicit block styling restored.
- Native direct header buttons retained dark backgrounds: scoped white-header overrides added.
- Unsupported Dashboard icon: switched to the existing bundled app-grid icon.

These were implementation/browser repairs before the final source comparison, not falsely counted as source-image QA iterations.

Comparison history: the earlier gate was blocked solely by the expired source file. The reattached reference resolved that blocker on 13 October 2026. The first complete combined visual comparison found no actionable P0/P1/P2 differences in the agreed scope, so no visual code fixes were made during this final comparison pass. Prior responsiveness and interaction evidence remains valid; the final native report-menu and dashboard refresh checks were repeated with no console warnings/errors.

## Implementation checklist and remaining acceptance

1. Source restored, normalized and privately retained — completed.
2. Same-size full-view and focused combined comparisons opened — completed.
3. Native reporting access, refresh and final console checks — passed; no new code changes required.
4. Temporary viewport override reset; the original ERP tab remains open — completed.
5. Full Arabic/RTL, role-specific mobile UAT and broader ERP acceptance remain separate programme work, not blockers for this narrowly scoped theme/navigation update.

Follow-up polish: optional regrouping of the many Construction ERP entries may improve scanning as the module catalogue grows; it is not part of this palette/navigation-position request. No extra redesign or new modules were added.
