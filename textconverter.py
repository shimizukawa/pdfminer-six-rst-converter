from __future__ import annotations
from pathlib import Path
from types import FunctionType
import dataclasses
import enum
import logging
import re
import textwrap
import typing

from pdfminer.image import ImageWriter
from pdfminer.layout import (
    LTItem,
    LTPage,
    LTTextBox,
    LTTextBoxHorizontal,
    LTTextLineHorizontal,
)
from pdfminer.converter import TextConverter as TextConverterBase


log = logging.getLogger(__name__)

font_warning_cache = {}


def font_warning(fontname, fontsize, current_line):
    if current_line in font_warning_cache:
        return

    page = None
    elem = current_line
    while elem and page is None:
        page = getattr(elem, 'pageid', None)
        elem = getattr(elem, 'parent', None)

    font_warning_cache[current_line] = (fontname, fontsize)
    log.warning('Unsupported font: %r(%r) for: (page %r) %r', fontname, fontsize, page, current_line.get_text())


InlineStyle = typing.Literal['normal', 'strong', 'em', 'code', 'term']


@dataclasses.dataclass
class InlineElement:
    parent: BlockElement
    style: InlineStyle = 'normal'
    text_stack: list[str] = dataclasses.field(default_factory=list)

    def __repr__(self):
        return f'{self.__class__.__name__}(style={self.style}, {self.raw_text[:20]!r})'

    def push_text(self, text: str) -> None:
        self.text_stack.append(text)

    @property
    def raw_text(self):
        text = ''.join(self.text_stack)
        text = re.sub(r'(\w)- (\w)', r'\1-\2', text)  # Self- Taught のような分断を結合
        if not self.style == 'code':
            text = re.sub(r' (\s+)', ' ', text)  # 連続する空白を除去
        return text

    def replace_inlines(self, text):
        text = re.sub(r'\bFigure (\d+)\.(\d+)\b', r':numref:`figure-\1-\2`', text) 
        return text

    def render(self, in_code=False):
        text = self.raw_text
        pre_s, text, post_s = re.match(r'^(\s*)(.*?)(\s*)$', text, re.DOTALL).groups()

        match self.style:
            case 'normal' | 'lineblock' | 'list-item':
                text = self.replace_inlines(text)
            case 'strong':
                text = f'**{text}**'
            case 'em':
                text = f'*{text}*'
            case 'code' if not in_code:
                if not re.match(r'^https?://', text):  # except URL
                    text = f'``{text}``'
            case 'code' | 'header' | 'figure' | 'figure-comment' | 'toc' | 'part' | 'h1' | 'h2' | 'h3':
                pass  # as is
            case _:
                log.warning('Unknow font style: %r', self.style)

        if in_code:
            text = pre_s + text + post_s
        return text


def trim_linebreak(text: str):
    # remove '\n'
    text = text.replace('-\n', '')
    text = re.sub(r'([^ ])\n([^ ])', r'\1 \2', text)
    text = text.replace('\n', '')
    return text


BlockStyle = typing.Literal['part', 'code', 'h1', 'h2', 'h3', 'paragraph', 'lineblock', 'header', 'figure', 'figure-comment', 'toc', 'list-item']


@dataclasses.dataclass
class BlockElement:
    parent: ChapterElement
    item: LTTextBoxHorizontal
    _style: BlockStyle = None
    inlines: list[InlineElement] = dataclasses.field(default_factory=list, repr=False, init=False)
    page: LTPage = dataclasses.field(default=None, repr=False)

    def __repr__(self):
        if not self.item:
            return f'{self.__class__.__name__}({id(self)}, None)'
        return f'{self.__class__.__name__}({id(self)}, x0={self.item.x0:.4f}, y0={self.item.y0:.4f}, style={self.style}, content={self.render_text()[:20]!r})'

    def __len__(self):
        return len(self.inlines)

    def __bool__(self):
        return bool(self.inlines)

    def get_firstline_x(self):
        return list(self.item)[0].x0
    def set_inline_style(self, style: InlineStyle):
        if not self.inlines:
            self.inlines.append(InlineElement(self, style=style))
        elif self.inlines[-1].style != style:
            self.inlines.append(InlineElement(self, style=style))
        else:
            pass  # style is not changed.

    def push_text(self, text: str) -> None:
        if not self.inlines:
            self.inlines.append(InlineElement(self))
        self.inlines[-1].push_text(text)

    def merge(self, other: BlockElement) -> None:
        self.inlines.extend(other.inlines)

    def is_all_style(self, *styles: list[InlineStyle]):
        return all(i.style in styles for i in self.inlines)

    def is_any_style(self, *styles: list[InlineStyle]):
        return any(i.style in styles for i in self.inlines)

    def is_style(self, *, require: InlineStyle, accepts: list[InlineStyle]):
        subjects = [i.style for i in self.inlines]
        if require not in subjects:
            return False
        return all(s in (require, *accepts) for s in subjects)


    @property
    def style(self) -> BlockStyle:
        if self._style is not None:
            return self._style

        # guess
        if self.is_header:
            return 'header'
        if self.is_style(require='code', accepts=['strong']):
            return 'code'
        for s in ('figure', 'toc', 'lineblock',):
            if self.is_style(require=s, accepts=['header', 'code']):
                return s
        for s in ('code', 'part', 'h1', 'h2', 'h3', 'figure-comment', 'list-item'):
            if self.is_all_style(s):
                return s
        return 'paragraph'
    @style.setter
    def style(self, style: BlockStyle):
        if self._style is not None and self._style != style:
            log.warning('Block style is changed: %r -> %r for (page %r) %r',
                self._style, style, self.page.pageid, self.item.get_text())
        self._style = style

    @property
    def is_header(self):
      if self._style == 'header':
          return True
      elif self.item is None:
          return False
      return self.item.y0 > 610  # it seems a page header

    def render_code(self):
        if any(i.style == 'strong' for i in self.inlines):
            header = '.. parsed-literal::\n\n'
        else:
            header = '.. code-block::\n\n'
        text = ''.join(i.render(in_code=True) for i in self.inlines)
        suite = textwrap.indent(text, '   ').rstrip()
        return header + suite

    def render_figure(self):
        text = ''.join(i.raw_text for i in self.inlines)  # 1 line
        figname, figtitle = [t.strip() for t in text.split(':', 2)]
        figname = figname.lower().replace(' ', '-').replace('.', '-')
        header = f'.. figure:: images/{figname}.*\n   :name: {figname}\n\n'
        suite = textwrap.indent(figtitle, '   ').rstrip()
        return header + suite

    def render_glossary(self):
        # log.warning('glossary_mode: (page %r) %r', self.page.pageid, text)
        stack: list[list[str]] = []
        for inline in self.inlines:
            if inline.style == 'term':
                t = inline.raw_text.rstrip(':')
                stack.append([t])
            else:
                t = inline.render().lstrip(': ')
                stack[-1].append(t)

        texts = []
        for term, *descs in stack:
            # drop empty inline and insert ' ' between inlines
            desc = ' '.join(t for t in descs if t.strip())
            desc = trim_linebreak(desc)
            texts.append(f'{term}\n   {desc}')

        header = '.. glossary::\n\n'
        suite = textwrap.indent('\n\n'.join(texts), ' '*3)
        return header + suite

    def render_text(self):
        if self.is_header:
            return ''  # remove page header

        # remove style from single ' '
        stack: list[InlineElement] = []
        for i in self.inlines:
            match stack:
                case *_,i2,i1 if i1.raw_text == ' ' and i2.style == i.style:
                    i2.push_text(i1.raw_text)
                    i2.push_text(i.raw_text)
                    stack.pop()  # discard i1
                case _:
                    stack.append(i)

        # drop empty inline and insert ' ' between inlines
        text = ' '.join(t for t in (i.render() for i in stack) if t)
        text = trim_linebreak(text)
        return text

    def render(self) -> str:
        if self.item is None:
            return ''
        elif self.style == 'code':
            return self.render_code()
        elif self.style == 'figure':
            return self.render_figure()
        elif self.style == 'glossary':
            return self.render_glossary()

        text = self.render_text()
        match self.style:
            case 'figure-comment':
                text = '.. figure-comment: ' + text
            case 'toc':
                text = '.. toc-comment: ' + text
            case 'part':
                border = '#'*len(text)
                text = '\n'.join([border, text, border])
            case 'h1':
                border = '='*len(text)
                text = '\n'.join([border, text, border])
            case 'h2':
                border = '='*len(text)
                text = '\n'.join([text, border])
            case 'h3':
                border = '-'*len(text)
                text = '\n'.join([text, border])
            case 'list-item':
                # not worked. list item style will be overwrited by paragraph style by folloing chars.
                text = '* ' + text

        return text


@dataclasses.dataclass
class ChapterElement:
    blocks: list[BlockElement] = dataclasses.field(default_factory=list)
    page_blocks: list[BlockElement] = dataclasses.field(default_factory=list)

    def close_page(self) -> None:
        self.blocks.extend(sorted(self.page_blocks, key=lambda b: b.item.y0, reverse=True))
        self.page_blocks = []

    def new_block(self, box: LTTextBoxHorizontal, page: LTPage):
        if self.page_blocks and not self.page_blocks[-1]:
            # 最後のblockが空なら捨てる（あるいは上書きする必要ある？）
            self.page_blocks.pop()
        block = BlockElement(self, box, page=page)
        self.page_blocks.append(block)

    def merge_blocks(self) -> None:
        blocks = [BlockElement(self, None)]
        for b in self.blocks:
            prev = blocks[-1]
            if b.is_header:
                continue  # skip page header
            if b.style == 'code':
                if prev.style == 'code':
                    prev.merge(b)
                else:
                    blocks.append(b)
            else:  # text
                if prev.style == b.style == 'paragraph' and prev.item is not b.item and 47.9 <= round(b.get_firstline_x(), 1) <= 48.0:
                    # 1つのパラグラフが複数blockに分かれている
                    log.info('merge block: %r', b)
                    prev.merge(b)
                else:
                    blocks.append(b)
        self.blocks = blocks

    def merge_glossaries(self) -> None:
        blocks: list[BlockElement] = []
        for b0 in self.blocks:
            if not blocks:
                blocks.append(b0)
            else:
                b1 = blocks[-1]
                if b1.style == 'h2' and b1.render_text() == 'Vocabulary':
                    # first glossary block
                    b0.style = 'glossary'
                    b0.inlines[0].style = 'term'  # overwrite style
                    blocks.append(b0)
                elif b0.style in ('part', 'h1', 'h2', 'h3'):
                    # after glossary
                    blocks.append(b0)
                elif b1.style == 'glossary':
                    # glossary continue
                    b0.inlines[0].style = 'term'  # overwrite style
                    b1.merge(b0)
                else:
                    # non-glossary
                    blocks.append(b0)
        self.blocks = blocks

    def get_block_style(self):
        return self.page_blocks[-1].style

    def set_inline_style(self, style):
        self.page_blocks[-1].set_inline_style(style)

    def push_text(self, text: str) -> None:
        self.page_blocks[-1].push_text(text)

    def render(self):
        self.merge_blocks()
        self.merge_glossaries()
        texts = []
        for b in self.blocks:
            # log.warning(b)
            texts.extend([b.render(), '\n\n'])
        return ''.join(texts)


# <LTTextBoxHorizontal(7) 60.000,363.008,165.462,383.006 'for i in range(1, 6):\n    print(i)\n'>
#     <LTTextLineHorizontal 60.000,519.178,165.462,528.178 'for i in range(100): \n'>
#         <LTChar 60.000,519.178,65.022,528.178 matrix=[8.37,0.00,0.00,9.00, (60.00,521.77)] font='BCXPYQ+LetterGothicStd' adv=0.6 text='f'>
# <LTTextBoxHorizontal(9) 47.998,119.924,231.549,130.424 'execute. In this case, it took 0.15 seconds.\n'>


class Font(str, enum.Enum):
    CODE      = 'BCXPYQ+LetterGothicStd'
    HEADING   = 'HDWEEE+StoneSansStd-Medium'
    HEADER    = 'TAVVUB+StoneSansStd-Bold'
    PARAGRAPH = 'RAZMOK+BerkeleyStd-Medium'
    STRONG    = 'BCXPYQ+BerkeleyStd-Bold'
    CODE_BOLD = 'NQEGLY+LetterGothicStd-Bold'
    EM        = 'VGXSUC+BerkeleyStd-Italic'
    FIGURE_C1 = 'TFAXUR+HelveticaLTStd-BoldCond'
    FIGURE_C2 = 'TFAXUR+HelveticaLTStd-Cond'
    FIGURE_C3 = 'TFAXUR+HelveticaLTStd-BoldCondObl'
    FIGURE_C4 = 'IXTELN+TektonPro-Bold'
    FIGURE_C5 = 'BSQHZM+HelveticaLTStd-CondObl'
    FIGURE_C6 = 'HYERGJ+HelveticaLTStd-Roman'
    TOC1      = 'HDWEEE+StoneSansStd-Medium'
    TOC2      = 'TAVVUB+StoneSansStd-Semibold'
    LIST_ITEM = 'OXPSJB+ZapfDingbatsStd'


@dataclasses.dataclass
class Visitor:
    imagewriter: ImageWriter|None = None
    chap: ChapterElement = dataclasses.field(default_factory=ChapterElement)
    current_page: LTPage = dataclasses.field(default=None, init=False, repr=False)
    current_box: LTTextBoxHorizontal = dataclasses.field(default=None, init=False, repr=False)
    current_line: LTTextLineHorizontal = dataclasses.field(default=None, init=False, repr=False)

    def walk(self, item: LTItem):
        self.dispatch(item)

    def get_text(self):
        self.chap.close_page()
        return self.chap.render()

    def dispatch(self, item: LTItem):
        for visit in self.get_functions(item, 'visit'):
            visit(item)
        for depart in self.get_functions(item, 'depart'):
            depart(item)

    def get_functions(self, item: LTItem, prefix: str) -> FunctionType|None:
        # print(prefix, item.__class__.mro())
        for c in item.__class__.mro():
            if c.__module__ != 'pdfminer.layout':
                continue
            visit = getattr(self, f'{prefix}_{c.__name__}', None)
            if visit is not None:
                yield visit
        return None

    def push_text(self, text):
        text = text.replace('\xa0', ' ')
        self.chap.push_text(text)

    def visit_LTTextBoxHorizontal(self, item: LTItem) -> None:
        # log.warning("%s-%d (%f,%f):%s", self.current_page.pageid, item.index, item.x0, item.y0, item.get_text().split('\n')[0][:20])
        self.current_box = item
        self.chap.new_block(self.current_box, self.current_page)

    def visit_LTTextLineHorizontal(self, item: LTItem) -> None:
        if self.current_line and round(self.current_line.x0) < round(item.x0):
            # 前の行よりも大きい（インデントしている）ので、新規blockにしたい
            self.chap.new_block(self.current_box, self.current_page)
        elif self.chap.get_block_style() == 'lineblock':
            # 前の行がlineblockなら新しい行も新規blockにする
            self.chap.new_block(self.current_box, self.current_page)
        self.current_line = item

    def visit_LTPage(self, item: LTItem) -> None:
        self.chap.close_page()
        self.current_page = item

    def visit_LTContainer(self, item: LTItem) -> None:
        for child in item:
            child.parent = item
            self.dispatch(child)

    def visit_LTChar(self, item: LTItem) -> None:
        match item.fontname, round(item.size, 1):
            case Font.CODE, _:
                self.chap.set_inline_style('code')
            case Font.HEADING, 50:  # title
                self.chap.set_inline_style('normal')
            case Font.HEADING, 40:
                self.chap.set_inline_style('part')
            case Font.HEADING, 30 | 28:
                self.chap.set_inline_style('h1')
            case Font.HEADING, 18:
                self.chap.set_inline_style('h2')
            case Font.HEADING, 15:
                self.chap.set_inline_style('h3')
            case Font.HEADING, 14.5:
                self.chap.set_inline_style('lineblock')
            case Font.HEADING, 9.0:
                self.chap.set_inline_style('figure')
            case Font.HEADER, _:
                self.chap.set_inline_style('header')
            case Font.PARAGRAPH, _:
                self.chap.set_inline_style('normal')
            case Font.STRONG | Font.CODE_BOLD, _:
                self.chap.set_inline_style('strong')
            case Font.EM, _:
                self.chap.set_inline_style('em')
            case Font.FIGURE_C1 | Font.FIGURE_C2 | Font.FIGURE_C3 | Font.FIGURE_C4 | Font.FIGURE_C5| Font.FIGURE_C6, _:
                self.chap.set_inline_style('figure-comment')
            case Font.TOC1 | Font.TOC2, 12 | 10:
                self.chap.set_inline_style('toc')
            case Font.LIST_ITEM, _:
                self.chap.set_inline_style('list-item')
            case fontname, fontsize:
                # unknown
                if self.chap.get_block_style() != 'header':
                    font_warning(fontname, fontsize, self.current_line)
                self.chap.set_inline_style('normal')

        self.push_text(item.get_text())

    def visit_LTAnno(self, item: LTItem) -> None:
        self.push_text(item.get_text())

    def visit_LTImage(self, item: LTItem) -> None:
        if self.imagewriter is None:
            return
        filename = self.imagewriter.export_image(item)
        # # write filename to output.
        # # comment-out: The certain pdf doesn't have valid images.
        # if self.current_box is None:
        #     log.warning('Drop image befor page: %r', filename)
        # else:
        #     self.chap.new_block(self.current_box, self.current_page)
        #     self.push_text(filename)


class RstConverter(TextConverterBase):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.visitor = Visitor(kwargs['imagewriter'])

    def receive_layout(self, ltpage: LTPage) -> None:
        self.visitor.walk(ltpage)

    def close(self) -> None:
        text = self.visitor.get_text()
        self.write_text(text)


class SplitRstConverter(TextConverterBase):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.visitor = Visitor(kwargs['imagewriter'])
        self.base_path = Path(Path(self.outfp.name).absolute().stem)
        self.base_path.mkdir(parents=True, exist_ok=True)

    def receive_layout(self, ltpage: LTPage) -> None:
        if ltpage.pageid > 218:  # skip index pages of the certain pdf
            if ltpage.pageid == 219:
                text = ''.join(i.get_text() for i in ltpage if isinstance(i, LTTextBox))
                log.warning('Drop after page (%r): %r...', ltpage.pageid, text[:30])
            return
        self.visitor.walk(ltpage)

    def close(self) -> None:
        chap = self.visitor.chap
        chap.close_page()
        chap.merge_blocks()
        chap.merge_glossaries()
        
        part_counter = 0
        chap_counter = 0
        for b in chap.blocks:
            match b.style:
                case 'part':
                    fname = f'Part{part_counter}.rst'
                    self.outfp = (self.base_path / fname).open('wb')
                    part_counter += 1
                case 'h1' if part_counter != 1:
                    chap_counter += 1
                    t = b.render_text().replace('?', '').replace(':', '').replace(' ', '-')
                    fname = f'Chap{chap_counter:02}-{t}.rst'
                    self.outfp = (self.base_path / fname).open('wb')
            self.write_text(b.render())
            self.write_text('\n\n')