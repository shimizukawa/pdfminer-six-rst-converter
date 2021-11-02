from __future__ import annotations
from types import FunctionType
import dataclasses
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


@dataclasses.dataclass
class InlineElement:
    style: typing.Literal['normal', 'strong', 'em', 'code'] = 'normal'
    text_stack: list[str] = dataclasses.field(default_factory=list)

    def __repr__(self):
        return f'{self.__class__.__name__}(style={self.style}, {self.raw_text[:20]!r})'

    def push_text(self, text: str) -> None:
        self.text_stack.append(text)

    @property
    def raw_text(self):
        text = ''.join(self.text_stack)
        return text

    def render(self):
        text = self.raw_text
        match self.style:
            case 'normal':
                return text
            case 'strong':
                return f' **{text.strip()}** '
            case 'em':
                return f' *{text.strip()}* '
            case 'code':
                return f' ``{text.strip()}`` '
            case _:
                log.warning('Unknow font style: %r', self.style)
                return text


@dataclasses.dataclass
class BlockElement:
    item: LTTextBoxHorizontal
    style: typing.Literal['code', 'h1', 'h2', 'h3', 'paragraph', 'lineblock', 'header'] = None
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
            self.inlines.append(InlineElement(style=style))
        elif self.inlines[-1].style != style:
            self.inlines.append(InlineElement(style=style))
        else:
            pass  # style is not changed.

    def push_text(self, text: str) -> None:
        if not self.inlines:
            self.inlines.append(InlineElement())
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
        header = '.. code-block::\n\n'
        text = ''.join(i.raw_text for i in self.inlines)
        suite = textwrap.indent(text, '   ').rstrip()
        return header + suite

    def render_text(self):
        if self.is_header:
            return ''  # remove page header
        stack: list[InlineElement] = []
        for i in self.inlines:
            match stack:
                case *_,i2,i1 if i1.raw_text == ' ' and i2.style == i.style:
                    i2.push_text(i1.raw_text)
                    i2.push_text(i.raw_text)
                    stack.pop()  # discard i1
                case _:
                    stack.append(i)

        text = ''.join(i.render() for i in stack)
        text = re.sub(r'\n', '', re.sub(r'([^ ])\n([^ ])', r'\1 \2', text)) 
        return text

    def render(self) -> str:
        if self.item is None:
            return ''
        elif self.style == 'code':
            return self.render_code()

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
        block = BlockElement(box, page=page)
        self.page_blocks.append(block)

    def merge_blocks(self) -> None:
        blocks = [BlockElement(None)]
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
                    log.warning('merge block: %r', b)
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
            case 'BCXPYQ+LetterGothicStd', _:
                self.chap.set_block_style('code')  # inlineの一部がcodeの場合、最後の文字でparagraphに戻る
                self.chap.set_inline_style('code')
            case 'HDWEEE+StoneSansStd-Medium', 40:
                self.chap.set_block_style('part')
            case 'HDWEEE+StoneSansStd-Medium', 30 | 28:
                self.chap.set_block_style('h1')
            case 'HDWEEE+StoneSansStd-Medium', 18:
                self.chap.set_block_style('h2')
            case 'HDWEEE+StoneSansStd-Medium', 15:
                self.chap.set_block_style('h3')
            case 'HDWEEE+StoneSansStd-Medium', 14.5:
                self.chap.set_block_style('lineblock')
            case 'TAVVUB+StoneSansStd-Bold', _:
                self.chap.set_block_style('header')
            case 'RAZMOK+BerkeleyStd-Medium', _:
                self.chap.set_block_style('paragraph')
                self.chap.set_inline_style('normal')
            case 'BCXPYQ+BerkeleyStd-Bold', _:
                self.chap.set_inline_style('strong')
            case 'VGXSUC+BerkeleyStd-Italic', _:
                self.chap.set_inline_style('em')
            case fontname, fontsize:
                # unknown
                if self.chap.get_block_style() != 'header':
                    log.warning('Unsupported font: %r(%r) for %r', fontname, fontsize, self.current_line)
                self.chap.set_block_style('paragraph')

        self.push_text(item.get_text())

    def visit_LTAnno(self, item: LTItem) -> None:
        self.push_text(item.get_text())

    def visit_LTImage(self, item: LTItem) -> None:
        if self.imagewriter is not None:
            self.imagewriter.export_image(item)


class TextConverter(TextConverterBase):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.visitor = Visitor(kwargs['imagewriter'])

    def receive_layout(self, ltpage: LTPage) -> None:
        self.visitor.walk(ltpage)
        return

    def close(self) -> None:
        text = self.visitor.get_text()
        self.write_text(text)
        return