import { expect, test, type Page } from "@playwright/test";
import JSZip from "jszip";

/**
 * Put an EPUB in front of the reader the way the product does it today.
 *
 * The reader used to be opened from `/chat?capability=immersive_reading`, where
 * the page itself offered a file input. Documents now live in the reading
 * library: a collection is created, a file is added to it, and the collection is
 * opened. This helper walks that supported flow so the spec keeps exercising the
 * product's real entry point rather than a removed one.
 */
async function uploadEpubToNewCollection(
  page: Page,
  filename: string,
  buffer: Buffer,
) {
  await page.goto("/learning/reading", { waitUntil: "domcontentloaded" });
  await page.getByRole("button", { name: "New collection", exact: true }).first().click();
  const dialog = page.locator('[role="dialog"]');
  await dialog.locator('input[aria-label="Collection name"]').fill(`EPUB audit ${Date.now()}`);
  await dialog.getByRole("button", { name: "Create", exact: true }).click();

  // Creating a collection lands on its page, which is where files are added.
  await expect(page).toHaveURL(/\/learning\/reading\/folders\//);
  await page.getByRole("button", { name: "Add material", exact: true }).click();
  await page.locator('input[type="file"]').first().setInputFiles({
    name: filename,
    mimeType: "application/epub+zip",
    buffer,
  });
  await page.getByRole("button", { name: "Add to a collection", exact: true }).click();

  // "Start reading" appears once the file is stored and added to the collection.
  await page.getByRole("link", { name: "Start reading" }).click();
  await expect(page).toHaveURL(/\/learning\/reading\/rw_/);
}

/** The document itself renders in the reader's frame. */
function readerFrame(page: Page) {
  return page.locator("iframe").contentFrame();
}

/** Open the collection panel, which starts closed. */
async function openContents(page: Page) {
  await page.getByRole("button", { name: "Expand contents" }).click();
  await expect(page.getByRole("tab", { name: "Contents" })).toBeVisible();
}

async function illustratedEpub(): Promise<Buffer> {
  const zip = new JSZip();
  zip.file("mimetype", "application/epub+zip", { compression: "STORE" });
  zip.file(
    "META-INF/container.xml",
    "<?xml version='1.0'?><container xmlns='urn:oasis:names:tc:opendocument:xmlns:container'><rootfiles><rootfile full-path='OPS/book.opf' media-type='application/oebps-package+xml'/></rootfiles></container>",
  );
  zip.file(
    "OPS/book.opf",
    "<?xml version='1.0'?><package xmlns='http://www.idpf.org/2007/opf' xmlns:dc='http://purl.org/dc/elements/1.1/' version='3.0' unique-identifier='book-id'><metadata><dc:identifier id='book-id'>urn:uuid:deeptutor-reader-test</dc:identifier><dc:title>Faithful reader</dc:title><dc:language>en</dc:language></metadata><manifest><item id='nav' href='nav.xhtml' media-type='application/xhtml+xml' properties='nav'/><item id='one' href='one.xhtml' media-type='application/xhtml+xml'/><item id='two' href='two.xhtml' media-type='application/xhtml+xml'/><item id='dot' href='dot.png' media-type='image/png'/></manifest><spine><itemref idref='one'/><itemref idref='two'/></spine></package>",
  );
  zip.file(
    "OPS/nav.xhtml",
    "<html xmlns='http://www.w3.org/1999/xhtml' xmlns:epub='http://www.idpf.org/2007/ops'><body><nav epub:type='toc'><ol><li><a href='one.xhtml'>Illustrated chapter</a></li><li><a href='two.xhtml'>Second chapter</a></li></ol></nav></body></html>",
  );
  zip.file(
    "OPS/one.xhtml",
    `<html xmlns='http://www.w3.org/1999/xhtml'><head><title>Illustrated chapter</title></head><body><h1 id='publisher-title'>Illustrated chapter</h1><h2>Source layout</h2><h3 id='late-detail'>Late detail</h3><p>This layout comes from the EPUB.</p><img alt='source illustration' src='dot.png'/></body></html>`,
  );
  zip.file(
    "OPS/two.xhtml",
    "<html xmlns='http://www.w3.org/1999/xhtml'><head><title>Second chapter</title></head><body><h1>Second chapter</h1><p>Keyboard navigation reached the second spine item.</p></body></html>",
  );
  zip.file(
    "OPS/dot.png",
    Buffer.from(
      "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
      "base64",
    ),
  );
  return zip.generateAsync({
    type: "nodebuffer",
    mimeType: "application/epub+zip",
  });
}

test("EPUB headings render in the reader and its chapter list navigates", async ({
  page,
}, testInfo) => {
  const filename = `epub-page-headings-${Date.now()}-${testInfo.project.name}.epub`;
  // The tall-page variant existed to drive in-page heading jumps, which the
  // reader no longer offers: the collection panel lists the document's spine,
  // so its chapters are what a learner navigates.
  await uploadEpubToNewCollection(page, filename, await illustratedEpub());

  const frame = readerFrame(page);
  // The EPUB's own markup is what the reader shows, headings included.
  await expect(
    frame.getByRole("heading", { name: "Illustrated chapter" }),
  ).toBeVisible();
  await expect(frame.getByRole("heading", { name: "Late detail" })).toBeVisible();

  // The collection panel lists the document's chapters, not invented rows, and
  // choosing one moves the reader to that spine item.
  await openContents(page);
  const secondChapter = page.getByRole("button", { name: /Second chapter/ });
  await expect(secondChapter).toBeVisible();
  await secondChapter.click();
  await expect(
    frame.getByRole("heading", { name: "Second chapter" }),
  ).toBeVisible();
  await expect(page.getByText(/Chapter 2/)).toBeVisible();
});

test("faithfully renders EPUB resources, navigates, and restores its last chapter", async ({
  page,
}, testInfo) => {
  const filename = `faithful-reader-${Date.now()}-${testInfo.project.name}.epub`;
  await uploadEpubToNewCollection(page, filename, await illustratedEpub());

  const frame = readerFrame(page);
  await expect(
    frame.getByRole("heading", { name: "Illustrated chapter" }),
  ).toBeVisible();
  // The packaged resource renders from the archive, not from a fallback.
  await expect(frame.getByAltText("source illustration")).toBeVisible();

  await openContents(page);
  await page.getByRole("button", { name: /Second chapter/ }).click();
  await expect(
    frame.getByRole("heading", { name: "Second chapter" }),
  ).toBeVisible();

  // The reader reports the spine position it moved to.
  await expect
    .poll(async () => {
      const response = await page.request.get("/api/reading/materials");
      const rows = (await response.json()) as Array<{
        material_id: string;
        filename: string;
      }>;
      const material = rows.find((row) => row.filename === filename);
      if (!material) return 0;
      const position = await page.request.get(
        `/api/reading/materials/${material.material_id}/position`,
      );
      return ((await position.json()) as { locator: number }).locator;
    })
    .toBe(2);

  // Reopening the document puts the reader back on that chapter.
  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(
    readerFrame(page).getByRole("heading", { name: "Second chapter" }),
  ).toBeVisible();
});
