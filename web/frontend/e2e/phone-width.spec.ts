import { expect, test, type Page } from "@playwright/test";

/**
 * The New Analysis page must fit a phone screen.
 *
 * On 2026-11-14 a user on a phone saw "Sign out" cut off at the right edge
 * and the date box running past the card. The header sat in one row with
 * fixed gaps, and the page kept 32px side padding at every width.
 */

const PHONE = { width: 390, height: 844 }; // iPhone 14/15

test.use({ viewport: PHONE, hasTouch: true, isMobile: true });

async function signIn(page: Page) {
  await page.goto("/login");
  await page.getByLabel("Username").fill("test-admin");
  await page.getByLabel("Password").fill("password");
  await page.getByRole("button", { name: /sign in/i }).click();
  await expect(page).toHaveURL(/\/new$/);
}

async function rightEdge(page: Page, name: string, locator = page.locator(name)) {
  const box = await locator.boundingBox();
  expect(box, `${name} not rendered`).not.toBeNull();
  return box!.x + box!.width;
}

test("New Analysis fits a phone screen", async ({ page }) => {
  await signIn(page);
  await expect(page.locator("#analysis_date")).toBeVisible();

  const { scrollWidth, innerWidth } = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    innerWidth: window.innerWidth,
  }));
  expect(scrollWidth, "page scrolls sideways").toBeLessThanOrEqual(innerWidth);

  const signOut = page.getByRole("button", { name: /sign out/i });
  expect(await rightEdge(page, "Sign out", signOut)).toBeLessThanOrEqual(
    PHONE.width,
  );

  // The date box must stay inside its card, not just inside the screen.
  const card = page.locator("form").locator("..");
  const cardRight = await rightEdge(page, "form card", card);
  expect(await rightEdge(page, "#analysis_date")).toBeLessThanOrEqual(cardRight);
});
