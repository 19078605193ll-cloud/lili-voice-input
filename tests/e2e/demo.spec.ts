import { expect, test } from "@playwright/test";

for (const width of [320, 375, 414, 768]) {
  test(`demo has no horizontal overflow at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/demo/");
    await expect(page.getByRole("heading", { name: "验证语音链路。" })).toBeVisible();
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
    expect(overflow).toBe(false);
  });
}

test("final result is integrated into the recording workspace", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/demo/");

  const workspace = page.locator(".workspace");
  const resultSection = workspace.locator(".result-section");
  const stopButton = workspace.locator("#stop-button");

  await expect(resultSection).toHaveCount(1);
  await expect(resultSection).toBeVisible();
  await expect(resultSection.locator("#result-text")).toBeVisible();
  await expect(resultSection.locator("#copy-button")).toBeVisible();
  await expect(resultSection.locator("#clear-button")).toBeVisible();
  await expect(page.locator("main > .result-section")).toHaveCount(0);

  const stopBox = await stopButton.boundingBox();
  const resultBox = await resultSection.boundingBox();
  expect(stopBox).not.toBeNull();
  expect(resultBox).not.toBeNull();
  expect(resultBox!.y).toBeGreaterThanOrEqual(stopBox!.y + stopBox!.height);

  const resultText = resultSection.locator("#result-text");
  await resultText.fill("待清空的转写结果");
  await resultSection.locator("#clear-button").click();
  await expect(resultText).toHaveValue("");
});
