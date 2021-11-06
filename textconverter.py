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
from pdfminer.layout import LTItem
from pdfminer.layout import LTPage
from pdfminer.layout import LTTextBoxHorizontal, LTTextLineHorizontal
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


@dataclasses.dataclass
class InlineElement:
    parent: BlockElement
    style: typing.Literal['normal', 'strong', 'em', 'code'] = 'normal'
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
            case 'normal':
                text = self.replace_inlines(text)
            case 'strong':
                text = f'**{text}**'
            case 'em':
                text = f'*{text}*'
            case 'code' if not in_code:
                if not re.match(r'^https?://', text):  # except URL
                    text = f'``{text}``'
            case 'code':
                pass  # as is
            case _:
                log.warning('Unknow font style: %r', self.style)

        if in_code:
            text = pre_s + text + post_s
        return text


@dataclasses.dataclass
class BlockElement:
    parent: ChapterElement
    item: LTTextBoxHorizontal
    style: typing.Literal['part', 'code', 'h1', 'h2', 'h3', 'paragraph', 'lineblock', 'header', 'figure', 'figure-comment', 'toc', 'list-item'] = None
    inlines: list[InlineElement] = dataclasses.field(default_factory=list, repr=False, init=False)
    page: LTPage = dataclasses.field(default=None, repr=False)

    def __post_init__(self):
        if self.item and self.item.x0 == 60.0:  # x0座標がcodeblockの位置っぽい
            self.style = 'code'

    def __repr__(self):
        if not self.item:
            return f'{self.__class__.__name__}({id(self)}, None)'
        return f'{self.__class__.__name__}({id(self)}, x0={self.item.x0:.4f}, y0={self.item.y0:.4f}, style={self.style}, content={self.render()[:20]!r})'

    def __len__(self):
        return len(self.inlines)

    def __bool__(self):
        return bool(self.inlines)

    def get_firstline_x(self):
        return list(self.item)[0].x0

    def set_inline_style(self, style):
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

    @property
    def is_header(self):
        return self.item.y0 > 610  # it seems a page header

    @property
    def is_code(self):
        return self.style == 'code'

    @property
    def is_heading(self):
        return self.style in ('h1', 'h2', 'h3')

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

        # insert ' ' between inlines
        text = ' '.join(i.render() for i in stack)

        # remove '\n'
        text = re.sub(r'\n', '', re.sub(r'([^ ])\n([^ ])', r'\1 \2', text)) 
        return text

    def render(self) -> str:
        if self.item is None:
            return ''
        elif self.style == 'code':
            return self.render_code()
        elif self.style == 'figure':
            return self.render_figure()
        elif self.style == 'figure-comment':
            return '.. figure-comment: ' + self.render_text().strip()
        elif self.style == 'toc':
            return '.. toc-comment: ' + self.render_text().strip()

        text = self.render_text()
        match self.style:
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
            if b.is_code:
                if prev.is_code:
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
                # blocks.append(b)
        self.blocks = blocks

    def set_block_style(self, style):
        self.page_blocks[-1].style = style

    def get_block_style(self):
        if self.page_blocks[-1].is_header:
            return 'header'
        return self.page_blocks[-1].style

    def set_inline_style(self, style):
        self.page_blocks[-1].set_inline_style(style)

    def push_text(self, text: str) -> None:
        self.page_blocks[-1].push_text(text)

    def render(self):
        self.merge_blocks()
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
                self.chap.set_block_style('code')  # inlineの一部がcodeの場合、最後の文字でparagraphに戻る
                self.chap.set_inline_style('code')
            case Font.HEADING, 50:  # title
                self.chap.set_block_style('paragraph')
            case Font.HEADING, 40:
                self.chap.set_block_style('part')
            case Font.HEADING, 30 | 28:
                self.chap.set_block_style('h1')
            case Font.HEADING, 18:
                self.chap.set_block_style('h2')
            case Font.HEADING, 15:
                self.chap.set_block_style('h3')
            case Font.HEADING, 14.5:
                self.chap.set_block_style('lineblock')
            case Font.HEADING, 9.0:
                self.chap.set_block_style('figure')
            case Font.HEADER, _:
                self.chap.set_block_style('header')
            case Font.PARAGRAPH, _:
                self.chap.set_block_style('paragraph')
                self.chap.set_inline_style('normal')
            case Font.STRONG | Font.CODE_BOLD, _:
                self.chap.set_inline_style('strong')
            case Font.EM, _:
                self.chap.set_inline_style('em')
            case Font.FIGURE_C1 | Font.FIGURE_C2 | Font.FIGURE_C3 | Font.FIGURE_C4 | Font.FIGURE_C5| Font.FIGURE_C6, _:
                self.chap.set_block_style('figure-comment')
            case Font.TOC1 | Font.TOC2, 12 | 10:
                self.chap.set_block_style('toc')
            case Font.LIST_ITEM, _:
                self.chap.set_block_style('list-item')
            case fontname, fontsize:
                # unknown
                if self.chap.get_block_style() != 'header':
                    font_warning(fontname, fontsize, self.current_line)
                self.chap.set_block_style('paragraph')

        self.push_text(item.get_text())

    def visit_LTAnno(self, item: LTItem) -> None:
        self.push_text(item.get_text())

    def visit_LTImage(self, item: LTItem) -> None:
        if self.imagewriter is not None:
            self.imagewriter.export_image(item)


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
        if ltpage.pageid > 195:  # skip index pages of the certain pdf
            return
        self.visitor.walk(ltpage)

    def close(self) -> None:
        chap = self.visitor.chap
        chap.close_page()
        chap.merge_blocks()
        
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