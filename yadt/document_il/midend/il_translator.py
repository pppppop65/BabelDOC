import concurrent.futures
import json

from tqdm import tqdm

from yadt.document_il import (
    Document,
    Page,
    PdfFormula,
    PdfParagraph,
    PdfParagraphComposition,
    PdfSameStyleCharacters,
    PdfSameStyleUnicodeCharacters,
    PdfStyle,
)
from yadt.document_il.translator.translator import BaseTranslator
from yadt.document_il.utils.layout_helper import (
    get_char_unicode_string,
    is_same_style,
)
from yadt.translation_config import TranslationConfig


class RichTextPlaceholder:
    def __init__(
        self,
        id: int,
        composition: PdfSameStyleCharacters,
        left_placeholder: str,
        right_placeholder: str,
    ):
        self.id = id
        self.composition = composition
        self.left_placeholder = left_placeholder
        self.right_placeholder = right_placeholder


class FormulaPlaceholder:
    def __init__(self, id: int, formula: PdfFormula, placeholder: str):
        self.id = id
        self.formula = formula
        self.placeholder = placeholder


class PbarContext:
    def __init__(self, pbar: tqdm):
        self.pbar = pbar

    def __enter__(self):
        return self.pbar

    def __exit__(self, exc_type, exc_value, traceback):
        self.pbar.update(1)


class DocumentTranslateTracker:
    def __init__(self):
        self.page = []

    def new_page(self):
        page = PageTranslateTracker()
        self.page.append(page)
        return page

    def to_json(self):
        pages = []
        for page in self.page:
            paragraphs = []
            for para in page.paragraph:
                i_str = getattr(para, "input", None)
                o_str = getattr(para, "output", None)
                pdf_unicode = getattr(para, "pdf_unicode", None)
                if pdf_unicode is None or i_str is None:
                    continue
                paragraphs.append(
                    {"input": i_str, "output": o_str, "pdf_unicode": pdf_unicode}
                )
            pages.append({"paragraph": paragraphs})
        return json.dumps({"page": pages}, ensure_ascii=False, indent=2)


class PageTranslateTracker:
    def __init__(self):
        self.paragraph = []

    def new_paragraph(self):
        paragraph = ParagraphTranslateTracker()
        self.paragraph.append(paragraph)
        return paragraph


class ParagraphTranslateTracker:
    def __init__(self):
        pass

    def set_pdf_unicode(self, unicode: str):
        self.pdf_unicode = unicode

    def set_input(self, input: str):
        self.input = input

    def set_output(self, output: str):
        self.output = output


class ILTranslator:
    def __init__(
        self,
        translate_engine: BaseTranslator,
        translation_config: TranslationConfig,
    ):
        self.translate_engine = translate_engine
        self.translation_config = translation_config

    def translate(self, docs: Document):
        tracker = DocumentTranslateTracker()
        # count total paragraph
        total = sum(len(page.pdf_paragraph) for page in docs.page)
        with tqdm(total=total, desc="translate") as pbar:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=self.translation_config.qps * 2
            ) as executor:
                for page in docs.page:
                    self.process_page(page, executor, pbar, tracker.new_page())

        path = self.translation_config.get_working_file_path("translate_tracking.json")

        with open(path, "w", encoding="utf-8") as f:
            f.write(tracker.to_json())

    def process_page(
        self,
        page: Page,
        executor: concurrent.futures.ThreadPoolExecutor,
        pbar: tqdm | None = None,
        tracker: PageTranslateTracker = None,
    ):
        for paragraph in page.pdf_paragraph:
            # self.translate_paragraph(paragraph, pbar)
            executor.submit(
                self.translate_paragraph, paragraph, pbar, tracker.new_paragraph()
            )

    class TranslateInput:
        def __init__(
            self,
            unicode: str,
            placeholders: [RichTextPlaceholder | FormulaPlaceholder],
            base_style: PdfStyle = None,
        ):
            self.unicode = unicode
            self.placeholders = placeholders
            self.base_style = base_style

    def create_formula_placeholder(
        self, formula: PdfFormula, id: int, paragraph: PdfParagraph
    ):
        placeholder = self.translate_engine.get_formular_placeholder(id)
        if placeholder in paragraph.unicode:
            return self.create_formula_placeholder(formula, id + 1, paragraph)

        return FormulaPlaceholder(id, formula, placeholder)

    def create_rich_text_placeholder(
        self,
        composition: PdfSameStyleCharacters,
        id: int,
        paragraph: PdfParagraph,
    ):
        left_placeholder = self.translate_engine.get_rich_text_left_placeholder(id)
        right_placeholder = self.translate_engine.get_rich_text_right_placeholder(id)
        if (
            left_placeholder in paragraph.unicode
            or right_placeholder in paragraph.unicode
        ):
            return self.create_rich_text_placeholder(
                composition,
                id + 1,
                paragraph,
            )

        return RichTextPlaceholder(
            id,
            composition,
            left_placeholder,
            right_placeholder,
        )

    def get_translate_input(self, paragraph: PdfParagraph):
        if not paragraph.pdf_paragraph_composition:
            return
        if len(paragraph.pdf_paragraph_composition) == 1:
            # 如果整个段落只有一个组成部分，那么直接返回，不需要套占位符等
            composition = paragraph.pdf_paragraph_composition[0]
            if (
                composition.pdf_line
                or composition.pdf_same_style_characters
                or composition.pdf_character
            ):
                return self.TranslateInput(paragraph.unicode, [], paragraph.pdf_style)
            elif composition.pdf_formula:
                # 不需要翻译纯公式
                return None
            else:
                raise ValueError(
                    f"Unknown composition type. "
                    f"Composition: {composition}. "
                    f"Paragraph: {paragraph}. "
                )

        placeholder_id = 1
        placeholders = []
        chars = []
        for composition in paragraph.pdf_paragraph_composition:
            if composition.pdf_line:
                chars.extend(composition.pdf_line.pdf_character)
            elif composition.pdf_formula:
                formula_placeholder = self.create_formula_placeholder(
                    composition.pdf_formula, placeholder_id, paragraph
                )
                placeholders.append(formula_placeholder)
                # 公式只需要一个占位符，所以 id+1
                placeholder_id = formula_placeholder.id + 1
                chars.extend(formula_placeholder.placeholder)
            elif composition.pdf_character:
                chars.append(composition.pdf_character)
            elif composition.pdf_same_style_characters:
                if is_same_style(
                    composition.pdf_same_style_characters.pdf_style,
                    paragraph.pdf_style,
                ):
                    chars.extend(composition.pdf_same_style_characters.pdf_character)
                    continue
                placeholder = self.create_rich_text_placeholder(
                    composition.pdf_same_style_characters,
                    placeholder_id,
                    paragraph,
                )
                placeholders.append(placeholder)
                # 样式需要一左一右两个占位符，所以 id+2
                placeholder_id = placeholder.id + 2
                chars.append(placeholder.left_placeholder)
                chars.extend(composition.pdf_same_style_characters.pdf_character)
                chars.append(placeholder.right_placeholder)
            else:
                raise Exception(
                    "Unexpected PdfParagraphComposition type "
                    "in PdfParagraph during translation. "
                    f"Composition: {composition}. "
                    f"Paragraph: {paragraph}. "
                )

        text = get_char_unicode_string(chars)
        return self.TranslateInput(text, placeholders, paragraph.pdf_style)

    def parse_translate_output(
        self, input: TranslateInput, output: str
    ) -> [PdfParagraphComposition]:
        import re

        result = []

        # 如果没有占位符，直接返回整个文本
        if not input.placeholders:
            comp = PdfParagraphComposition()
            comp.pdf_same_style_unicode_characters = PdfSameStyleUnicodeCharacters()
            comp.pdf_same_style_unicode_characters.unicode = output
            comp.pdf_same_style_unicode_characters.pdf_style = input.base_style
            return [comp]

        # 构建正则表达式模式
        patterns = []
        placeholder_map = {}

        for placeholder in input.placeholders:
            if isinstance(placeholder, FormulaPlaceholder):
                # 转义特殊字符
                pattern = re.escape(placeholder.placeholder)
                patterns.append(f"({pattern})")
                placeholder_map[placeholder.placeholder] = placeholder
            else:
                left = re.escape(placeholder.left_placeholder)
                right = re.escape(placeholder.right_placeholder)
                patterns.append(f"({left}.*?{right})")
                placeholder_map[placeholder.left_placeholder] = placeholder

        # 合并所有模式
        combined_pattern = "|".join(patterns)

        # 找到所有匹配
        last_end = 0
        for match in re.finditer(combined_pattern, output):
            # 处理匹配之前的普通文本
            if match.start() > last_end:
                text = output[last_end : match.start()]
                if text:
                    comp = PdfParagraphComposition()
                    comp.pdf_same_style_unicode_characters = (
                        PdfSameStyleUnicodeCharacters()
                    )
                    comp.pdf_same_style_unicode_characters.unicode = text
                    comp.pdf_same_style_unicode_characters.pdf_style = input.base_style
                    result.append(comp)

            matched_text = match.group(0)

            # 处理占位符
            if any(
                isinstance(p, FormulaPlaceholder) and matched_text == p.placeholder
                for p in input.placeholders
            ):
                # 处理公式占位符
                placeholder = next(
                    p
                    for p in input.placeholders
                    if isinstance(p, FormulaPlaceholder)
                    and matched_text == p.placeholder
                )
                comp = PdfParagraphComposition()
                comp.pdf_formula = placeholder.formula
                result.append(comp)
            else:
                # 处理富文本占位符
                placeholder = next(
                    p
                    for p in input.placeholders
                    if not isinstance(p, FormulaPlaceholder)
                    and matched_text.startswith(p.left_placeholder)
                )
                text = matched_text[
                    len(placeholder.left_placeholder) : -len(
                        placeholder.right_placeholder
                    )
                ]

                if isinstance(
                    placeholder.composition, PdfSameStyleCharacters
                ) and text.replace(" ", "") == "".join(
                    x.char_unicode for x in placeholder.composition.pdf_character
                ).replace(
                    " ", ""
                ):
                    comp = PdfParagraphComposition(
                        pdf_same_style_characters=placeholder.composition
                    )
                else:
                    comp = PdfParagraphComposition()
                    comp.pdf_same_style_unicode_characters = (
                        PdfSameStyleUnicodeCharacters()
                    )
                    comp.pdf_same_style_unicode_characters.pdf_style = (
                        placeholder.composition.pdf_style
                    )
                    comp.pdf_same_style_unicode_characters.unicode = text
                result.append(comp)

            last_end = match.end()

        # 处理最后的普通文本
        if last_end < len(output):
            text = output[last_end:]
            if text:
                comp = PdfParagraphComposition()
                comp.pdf_same_style_unicode_characters = PdfSameStyleUnicodeCharacters()
                comp.pdf_same_style_unicode_characters.unicode = text
                comp.pdf_same_style_unicode_characters.pdf_style = input.base_style
                result.append(comp)

        return result

    def translate_paragraph(
        self,
        paragraph: PdfParagraph,
        pbar: tqdm | None = None,
        tracker: ParagraphTranslateTracker = None,
    ):
        with PbarContext(pbar):
            if paragraph.vertical:
                return

            tracker.set_pdf_unicode(paragraph.unicode)

            translate_input = self.get_translate_input(paragraph)
            if not translate_input:
                return

            tracker.set_input(translate_input.unicode)

            text = translate_input.unicode
            translated_text = self.translate_engine.translate(text)

            tracker.set_output(translated_text)

            if translated_text == text:
                return

            paragraph.unicode = translated_text
            paragraph.pdf_paragraph_composition = self.parse_translate_output(
                translate_input, translated_text
            )
            for composition in paragraph.pdf_paragraph_composition:
                if (
                    composition.pdf_same_style_unicode_characters
                    and composition.pdf_same_style_unicode_characters.pdf_style is None
                ):
                    composition.pdf_same_style_unicode_characters.pdf_style = (
                        paragraph.pdf_style
                    )
