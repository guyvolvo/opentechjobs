import { expect, type Locator, type Page } from "@playwright/test";

/** The multiselects are custom widgets, so their internals get ids. */
export type MultiSelectName =
  | "department" | "seniority" | "company" | "location" | "workplace";

export class FiltersPanel {
  readonly page: Page;
  readonly toggle: Locator;
  readonly keywords: Locator;
  readonly datePosted: Locator;
  readonly sort: Locator;
  readonly starred: Locator;
  readonly israelOnly: Locator;
  readonly reset: Locator;

  constructor(page: Page) {
    this.page = page;
    this.toggle = page.locator("#filters-toggle");
    this.keywords = page.locator("#f-keywords");
    this.datePosted = page.locator("#f-date-posted");
    this.sort = page.locator("#f-sort");
    this.starred = page.locator("#f-starred");
    this.israelOnly = page.locator("#f-israel");
    this.reset = page.locator("#f-reset");
  }

  /** Collapsed below a container width; opening is a no-op when it is not. */
  async open() {
    if (await this.toggle.isVisible()) {
      const expanded = await this.toggle.getAttribute("aria-expanded");
      if (expanded !== "true") await this.toggle.click();
    }
  }

  multiSelect(name: MultiSelectName) {
    return new MultiSelect(this.page, name);
  }

  /** The count the Filters button advertises, or 0 when it shows none. */
  async activeCount(): Promise<number> {
    const text = (await this.toggle.textContent()) ?? "";
    const match = text.match(/\((\d+)\)/);
    return match ? Number(match[1]) : 0;
  }
}

export class MultiSelect {
  readonly root: Locator;
  readonly trigger: Locator;
  readonly menu: Locator;
  readonly options: Locator;
  readonly clear: Locator;
  readonly search: Locator;

  constructor(private page: Page, name: MultiSelectName) {
    this.root = page.locator(`#ms-${name}`);
    this.trigger = this.root.getByRole("button", { expanded: false }).first();
    this.menu = this.root.locator(".ms-menu");
    this.options = this.root.locator(".ms-options input[type=checkbox]");
    this.clear = this.root.getByRole("button", { name: /clear/i });
    this.search = this.root.getByPlaceholder(/filter/i);
  }

  async open() {
    const trigger = this.root.locator(".ms-toggle");
    if (await this.menu.isHidden()) await trigger.click();
    await expect(this.menu).toBeVisible();
  }

  /** Label text of every option currently listed. */
  async optionLabels(): Promise<string[]> {
    return this.root.locator(".ms-options label").allInnerTexts();
  }

  async selectNth(index: number): Promise<string> {
    await this.open();
    const box = this.options.nth(index);
    const label = (await box.locator("xpath=ancestor::label").innerText()).trim();
    await box.check();
    return label;
  }

  async label(): Promise<string> {
    return ((await this.root.locator(".ms-toggle").textContent()) ?? "").trim();
  }

  async clearAll() {
    await this.open();
    await this.clear.click();
  }
}
