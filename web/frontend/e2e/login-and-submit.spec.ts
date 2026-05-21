import { expect, test } from "@playwright/test";

/**
 * Happy-path e2e: login → submit a run → see the rating badge → logout.
 *
 * Backend is launched by `playwright.config.ts` with FAKE_LLM=1, so the
 * scripted `_fake_stream_run` finishes the run in well under 5 seconds
 * and emits `Rating: Buy` as the final decision (see
 * web/backend/app/services/run_service.py:_fake_stream_run).
 *
 * Credentials baked into playwright.config.ts:
 *   - username: test-admin
 *   - password: password   (bcrypt hash compiled into ADMIN_PASSWORD_HASH)
 */

const USERNAME = "test-admin";
const PASSWORD = "password";

test.describe("happy path", () => {
  test("login, submit a SPY run, observe a 5-tier rating, logout", async ({
    page,
  }) => {
    // ----- 1. Login --------------------------------------------------------
    await page.goto("/login");

    await page.getByLabel("Username").fill(USERNAME);
    await page.getByLabel("Password").fill(PASSWORD);
    await page.getByRole("button", { name: /sign in/i }).click();

    await expect(page).toHaveURL(/\/new$/);

    // ----- 2. Wait for catalog-driven defaults to seed --------------------
    // The form's useEffect chain (provider list → models → analysts) needs
    // a couple of network round-trips before submit-time validation passes.
    // We wait for every dropdown that drives validation to display a real
    // choice (not the "Loading…" / "Pick a …" placeholder).

    const providerCombo = page.getByRole("combobox", { name: /llm provider/i });
    const quickCombo = page.getByRole("combobox", { name: /quick-think model/i });
    const deepCombo = page.getByRole("combobox", { name: /deep-think model/i });
    const langCombo = page.getByRole("combobox", { name: /output language/i });
    // Each combobox renders its placeholder ("Pick a …" / "Loading …")
    // until the catalog response lands AND the form's useEffect seeds the
    // controlled state. Wait for a real choice in every required slot.
    const realChoice = /\S.{3,}/; // any non-trivial selected label
    for (const combo of [providerCombo, quickCombo, deepCombo, langCombo]) {
      await expect(combo).toHaveText(realChoice, { timeout: 20_000 });
      await expect(combo).not.toContainText(/loading|pick a/i);
    }
    // Confirm an analyst checkbox row rendered before we trust the
    // default-selection useEffect has run.
    await expect(page.getByLabel(/market analyst/i)).toBeVisible({
      timeout: 15_000,
    });

    // ----- 3. Fill the run form -------------------------------------------
    await page.getByLabel(/^ticker$/i).fill("SPY");

    // Accept all the other defaults. The form auto-seeds:
    //   - analysts (all available for asset_type=stock)
    //   - depth = 1, language = English
    //   - provider = first in catalog, quick/deep models = first in list
    //   - enable_checkpoint = true
    await page.getByRole("button", { name: /start analysis/i }).click();

    // ----- 4. Navigate to /runs/<uuid> ------------------------------------
    await expect(page).toHaveURL(
      /\/runs\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i,
      { timeout: 15_000 },
    );

    // ----- 5. Wait for the run to complete with a 5-tier rating ----------
    // The DecisionBadge renders `aria-label="Recommendation: <Rating>"`.
    // Matching by aria-label avoids coupling to surrounding text formatting.
    const ratings = ["Buy", "Overweight", "Hold", "Underweight", "Sell"];
    const badge = page.getByLabel(
      new RegExp(`Recommendation:\\s*(${ratings.join("|")})`, "i"),
    );
    await expect(badge).toBeVisible({ timeout: 30_000 });
    const badgeText = (await badge.textContent())?.trim() ?? "";
    expect(ratings).toContain(badgeText);

    // Status pill should land on "completed" once the fake stream is done.
    await expect(
      page.getByText(/^\s*completed\s*$/i).first(),
    ).toBeVisible({ timeout: 30_000 });

    // ----- 6. Sign out and return to /login -------------------------------
    await page.getByRole("button", { name: /sign out/i }).click();
    await expect(page).toHaveURL(/\/login$/, { timeout: 10_000 });
  });
});
