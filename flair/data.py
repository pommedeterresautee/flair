from abc import abstractmethod
from operator import itemgetter
from typing import List, Dict, Union, Callable

import torch, flair
import logging

from collections import Counter
from collections import defaultdict

from segtok.segmenter import split_single
from segtok.tokenizer import split_contractions
from segtok.tokenizer import word_tokenizer
from torch.utils.data import Dataset, random_split
from torch.utils.data.dataset import ConcatDataset, Subset

from flair.file_utils import Tqdm

log = logging.getLogger("flair")


class Dictionary:
    """
    This class holds a dictionary that maps strings to IDs, used to generate one-hot encodings of strings.
    """

    def __init__(self, add_unk=True):
        # init dictionaries
        self.item2idx: Dict[str, int] = {}
        self.idx2item: List[str] = []
        self.multi_label: bool = False

        # in order to deal with unknown tokens, add <unk>
        if add_unk:
            self.add_item("<unk>")

    def add_item(self, item: str) -> int:
        """
        add string - if already in dictionary returns its ID. if not in dictionary, it will get a new ID.
        :param item: a string for which to assign an id.
        :return: ID of string
        """
        item = item.encode("utf-8")
        if item not in self.item2idx:
            self.idx2item.append(item)
            self.item2idx[item] = len(self.idx2item) - 1
        return self.item2idx[item]

    def get_idx_for_item(self, item: str) -> int:
        """
        returns the ID of the string, otherwise 0
        :param item: string for which ID is requested
        :return: ID of string, otherwise 0
        """
        item = item.encode("utf-8")
        if item in self.item2idx.keys():
            return self.item2idx[item]
        else:
            return 0

    def get_idx_for_items(self, items: List[str]) -> List[int]:
        """
        returns the IDs for each item of the list of string, otherwise 0 if not found
        :param items: List of string for which IDs are requested
        :return: List of ID of strings
        """
        if not hasattr(self, "item2idx_not_encoded"):
            d = dict(
                [(key.decode("UTF-8"), value) for key, value in self.item2idx.items()]
            )
            self.item2idx_not_encoded = defaultdict(int, d)

        if not items:
            return []
        results = itemgetter(*items)(self.item2idx_not_encoded)
        if isinstance(results, int):
            return [results]
        return list(results)

    def get_items(self) -> List[str]:
        items = []
        for item in self.idx2item:
            items.append(item.decode("UTF-8"))
        return items

    def __len__(self) -> int:
        return len(self.idx2item)

    def get_item_for_index(self, idx):
        return self.idx2item[idx].decode("UTF-8")

    def save(self, savefile):
        import pickle

        with open(savefile, "wb") as f:
            mappings = {"idx2item": self.idx2item, "item2idx": self.item2idx}
            pickle.dump(mappings, f)

    @classmethod
    def load_from_file(cls, filename: str):
        import pickle

        dictionary: Dictionary = Dictionary()
        with open(filename, "rb") as f:
            mappings = pickle.load(f, encoding="latin1")
            idx2item = mappings["idx2item"]
            item2idx = mappings["item2idx"]
            dictionary.item2idx = item2idx
            dictionary.idx2item = idx2item
        return dictionary

    @classmethod
    def load(cls, name: str):
        from flair.file_utils import cached_path

        if name == "chars" or name == "common-chars":
            base_path = "https://s3.eu-central-1.amazonaws.com/alan-nlp/resources/models/common_characters"
            char_dict = cached_path(base_path, cache_dir="datasets")
            return Dictionary.load_from_file(char_dict)

        if name == "chars-large" or name == "common-chars-large":
            base_path = "https://s3.eu-central-1.amazonaws.com/alan-nlp/resources/models/common_characters_large"
            char_dict = cached_path(base_path, cache_dir="datasets")
            return Dictionary.load_from_file(char_dict)

        if name == "chars-xl" or name == "common-chars-xl":
            base_path = "https://s3.eu-central-1.amazonaws.com/alan-nlp/resources/models/common_characters_xl"
            char_dict = cached_path(base_path, cache_dir="datasets")
            return Dictionary.load_from_file(char_dict)

        return Dictionary.load_from_file(name)


class Label:
    """
    This class represents a label of a sentence. Each label has a value and optionally a confidence score. The
    score needs to be between 0.0 and 1.0. Default value for the score is 1.0.
    """

    def __init__(self, value: str, score: float = 1.0):
        self.value = value
        self.score = score
        super().__init__()

    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, value):
        if not value and value != "":
            raise ValueError(
                "Incorrect label value provided. Label value needs to be set."
            )
        else:
            self._value = value

    @property
    def score(self):
        return self._score

    @score.setter
    def score(self, score):
        if 0.0 <= score <= 1.0:
            self._score = score
        else:
            self._score = 1.0

    def to_dict(self):
        return {"value": self.value, "confidence": self.score}

    def __str__(self):
        return "{} ({})".format(self._value, self._score)

    def __repr__(self):
        return "{} ({})".format(self._value, self._score)


class DataPoint:
    @property
    @abstractmethod
    def embedding(self):
        pass

    @abstractmethod
    def to(self, device: str, pin_memory: bool = False):
        pass

    @abstractmethod
    def clear_embeddings(self, embedding_names: List[str] = None):
        pass


class DataPair(DataPoint):
    def __init__(self, first: DataPoint, second: DataPoint):
        self.first = first
        self.second = second

    def to(self, device: str, pin_memory: bool = False):
        self.first.to(device, pin_memory)
        self.second.to(device, pin_memory)

    def clear_embeddings(self, embedding_names: List[str] = None):
        self.first.clear_embeddings(embedding_names)
        self.second.clear_embeddings(embedding_names)

    def embedding(self):
        return torch.cat([self.first.embedding, self.second.embedding])

    def __str__(self):
        return f"DataPoint:\n first: {self.first}\n second: {self.second}"


class Token(DataPoint):
    """
    This class represents one word in a tokenized sentence. Each token may have any number of tags. It may also point
    to its head in a dependency tree.
    """

    def __init__(
        self,
        text: str,
        idx: int = None,
        head_id: int = None,
        whitespace_after: bool = True,
        start_position: int = None,
    ):
        self.text: str = text
        self.idx: int = idx
        self.head_id: int = head_id
        self.whitespace_after: bool = whitespace_after

        self.start_pos = start_position
        self.end_pos = (
            start_position + len(text) if start_position is not None else None
        )

        self.sentence: Sentence = None
        self._embeddings: Dict = {}
        self.tags: Dict[str, Label] = {}
        self.tags_proba_dist: Dict[str, List[Label]] = {}

    def add_tag_label(self, tag_type: str, tag: Label):
        self.tags[tag_type] = tag

    def add_tags_proba_dist(self, tag_type: str, tags: List[Label]):
        self.tags_proba_dist[tag_type] = tags

    def add_tag(self, tag_type: str, tag_value: str, confidence=1.0):
        tag = Label(tag_value, confidence)
        self.tags[tag_type] = tag

    def get_tag(self, tag_type: str) -> Label:
        if tag_type in self.tags:
            return self.tags[tag_type]
        return Label("")

    def get_tags_proba_dist(self, tag_type: str) -> List[Label]:
        if tag_type in self.tags_proba_dist:
            return self.tags_proba_dist[tag_type]
        return []

    def get_head(self):
        return self.sentence.get_token(self.head_id)

    def set_embedding(self, name: str, vector: torch.tensor):
        device = flair.device
        if (flair.embedding_storage_mode == "cpu") and len(self._embeddings.keys()) > 0:
            device = next(iter(self._embeddings.values())).device
        if device != vector.device:
            vector = vector.to(device)
        self._embeddings[name] = vector

    def to(self, device: str, pin_memory: bool = False):
        for name, vector in self._embeddings.items():
            if str(vector.device) != str(device):
                if pin_memory:
                    self._embeddings[name] = vector.to(
                        device, non_blocking=True
                    ).pin_memory()
                else:
                    self._embeddings[name] = vector.to(device, non_blocking=True)

    def clear_embeddings(self, embedding_names: List[str] = None):
        if embedding_names is None:
            self._embeddings: Dict = {}
        else:
            for name in embedding_names:
                if name in self._embeddings.keys():
                    del self._embeddings[name]

    def get_each_embedding(self) -> torch.tensor:
        embeddings = []
        for embed in sorted(self._embeddings.keys()):
            embed = self._embeddings[embed].to(flair.device)
            if (flair.embedding_storage_mode == "cpu") and embed.device != flair.device:
                embed = embed.to(flair.device)
            embeddings.append(embed)
        return embeddings

    def get_embedding(self) -> torch.tensor:
        embeddings = self.get_each_embedding()

        if embeddings:
            return torch.cat(embeddings, dim=0)

        return torch.tensor([], device=flair.device)

    @property
    def start_position(self) -> int:
        return self.start_pos

    @property
    def end_position(self) -> int:
        return self.end_pos

    @property
    def embedding(self):
        return self.get_embedding()

    def __str__(self) -> str:
        return (
            "Token: {} {}".format(self.idx, self.text)
            if self.idx is not None
            else "Token: {}".format(self.text)
        )

    def __repr__(self) -> str:
        return (
            "Token: {} {}".format(self.idx, self.text)
            if self.idx is not None
            else "Token: {}".format(self.text)
        )


class Span:
    """
    This class represents one textual span consisting of Tokens. A span may have a tag.
    """

    def __init__(self, tokens: List[Token], tag: str = None, score=1.0):
        self.tokens = tokens
        self.tag = tag
        self.score = score
        self.start_pos = None
        self.end_pos = None

        if tokens:
            self.start_pos = tokens[0].start_position
            self.end_pos = tokens[len(tokens) - 1].end_position

    @property
    def text(self) -> str:
        return " ".join([t.text for t in self.tokens])

    def to_original_text(self) -> str:
        pos = self.tokens[0].start_pos
        if pos is None:
            return " ".join([t.text for t in self.tokens])
        str = ""
        for t in self.tokens:
            while t.start_pos != pos:
                str += " "
                pos += 1

            str += t.text
            pos += len(t.text)

        return str

    def to_dict(self):
        return {
            "text": self.to_original_text(),
            "start_pos": self.start_pos,
            "end_pos": self.end_pos,
            "type": self.tag,
            "confidence": self.score,
        }

    def __str__(self) -> str:
        ids = ",".join([str(t.idx) for t in self.tokens])
        return (
            '{}-span [{}]: "{}"'.format(self.tag, ids, self.text)
            if self.tag is not None
            else 'span [{}]: "{}"'.format(ids, self.text)
        )

    def __repr__(self) -> str:
        ids = ",".join([str(t.idx) for t in self.tokens])
        return (
            '<{}-span ({}): "{}">'.format(self.tag, ids, self.text)
            if self.tag is not None
            else '<span ({}): "{}">'.format(ids, self.text)
        )


def space_tokenizer(text: str) -> List[Token]:
    """
    Tokenizer based on space character only.
    """
    tokens: List[Token] = []
    word = ""
    index = -1
    for index, char in enumerate(text):
        if char == " ":
            if len(word) > 0:
                start_position = index - len(word)
                tokens.append(
                    Token(
                        text=word, start_position=start_position, whitespace_after=True
                    )
                )

            word = ""
        else:
            word += char
    # increment for last token in sentence if not followed by whitespace
    index += 1
    if len(word) > 0:
        start_position = index - len(word)
        tokens.append(
            Token(text=word, start_position=start_position, whitespace_after=False)
        )
    return tokens


def segtok_tokenizer(text: str) -> List[Token]:
    """
    Tokenizer using segtok, a third party library dedicated to rules-based Indo-European languages.
    https://github.com/fnl/segtok
    """
    tokens: List[Token] = []

    words: List[str] = []
    sentences = split_single(text)
    for sentence in sentences:
        contractions = split_contractions(word_tokenizer(sentence))
        words.extend(contractions)

    # determine offsets for whitespace_after field
    index = text.index
    current_offset = 0
    previous_word_offset = -1
    previous_token = None
    for word in words:
        try:
            word_offset = index(word, current_offset)
            start_position = word_offset
        except:
            word_offset = previous_word_offset + 1
            start_position = (
                current_offset + 1 if current_offset > 0 else current_offset
            )

        token = Token(text=word, start_position=start_position, whitespace_after=True)
        tokens.append(token)

        if (previous_token is not None) and word_offset - 1 == previous_word_offset:
            previous_token.whitespace_after = False

        current_offset = word_offset + len(word)
        previous_word_offset = current_offset - 1
        previous_token = token

    return tokens


def build_spacy_tokenizer(model) -> Callable[[str], List[Token]]:
    """
    Wrap Spacy model to build a tokenizer for the Sentence class.
    :param model a Spacy V2 model
    :return a tokenizer function to provide to Sentence class constructor
    """
    try:
        from spacy.language import Language
        from spacy.tokens.doc import Doc
        from spacy.tokens.token import Token as SpacyToken
    except ImportError:
        raise ImportError(
            "Please install Spacy v2.0 or better before using the Spacy tokenizer, otherwise you can use segtok_tokenizer as advanced tokenizer."
        )

    model: Language = model

    def tokenizer(text: str) -> List[Token]:
        doc: Doc = model.make_doc(text)
        previous_token = None
        tokens: List[Token] = []
        for word in doc:
            word: SpacyToken = word
            token = Token(
                text=word.text, start_position=word.idx, whitespace_after=True
            )
            tokens.append(token)

            if (previous_token is not None) and (
                token.start_pos - 1
                == previous_token.start_pos + len(previous_token.text)
            ):
                previous_token.whitespace_after = False

            previous_token = token
        return tokens

    return tokenizer


class Sentence(DataPoint):
    """
       A Sentence is a list of Tokens and is used to represent a sentence or text fragment.
    """

    def __init__(
        self,
        text: str = None,
        use_tokenizer: Union[bool, Callable[[str], List[Token]]] = space_tokenizer,
        labels: Union[List[Label], List[str]] = None,
        language_code: str = None,
    ):
        """
        Class to hold all meta related to a text (tokens, predictions, language code, ...)
        :param text: original string
        :param use_tokenizer: a custom tokenizer (default is space based tokenizer,
        more advanced options are segtok_tokenizer to use segtok or build_spacy_tokenizer to use Spacy library
        if available). Check the code of space_tokenizer to implement your own (if you need it).
        If instead of providing a function, this parameter is just set to True, segtok will be used.
        :param labels:
        :param language_code:
        """
        super(Sentence, self).__init__()

        self.tokens: List[Token] = []

        self.labels: List[Label] = []
        if labels is not None:
            self.add_labels(labels)

        self._embeddings: Dict = {}

        self.language_code: str = language_code

        tokenizer = use_tokenizer
        if type(use_tokenizer) == bool:
            tokenizer = segtok_tokenizer if use_tokenizer else space_tokenizer

        # if text is passed, instantiate sentence with tokens (words)
        if text is not None:
            [self.add_token(token) for token in tokenizer(text)]

        # log a warning if the dataset is empty
        if text == "":
            log.warn(
                "ACHTUNG: An empty Sentence was created! Are there empty strings in your dataset?"
            )

        self.tokenized = None

    def get_token(self, token_id: int) -> Token:
        for token in self.tokens:
            if token.idx == token_id:
                return token

    def add_token(self, token: Union[Token, str]):

        if type(token) is str:
            token = Token(token)

        self.tokens.append(token)

        # set token idx if not set
        token.sentence = self
        if token.idx is None:
            token.idx = len(self.tokens)

    def get_spans(self, tag_type: str, min_score=-1) -> List[Span]:

        spans: List[Span] = []

        current_span = []

        tags = defaultdict(lambda: 0.0)

        previous_tag_value: str = "O"
        for token in self:

            tag: Label = token.get_tag(tag_type)
            tag_value = tag.value

            # non-set tags are OUT tags
            if tag_value == "" or tag_value == "O":
                tag_value = "O-"

            # anything that is not a BIOES tag is a SINGLE tag
            if tag_value[0:2] not in ["B-", "I-", "O-", "E-", "S-"]:
                tag_value = "S-" + tag_value

            # anything that is not OUT is IN
            in_span = False
            if tag_value[0:2] not in ["O-"]:
                in_span = True

            # single and begin tags start a new span
            starts_new_span = False
            if tag_value[0:2] in ["B-", "S-"]:
                starts_new_span = True

            if (
                previous_tag_value[0:2] in ["S-"]
                and previous_tag_value[2:] != tag_value[2:]
                and in_span
            ):
                starts_new_span = True

            if (starts_new_span or not in_span) and len(current_span) > 0:
                scores = [t.get_tag(tag_type).score for t in current_span]
                span_score = sum(scores) / len(scores)
                if span_score > min_score:
                    spans.append(
                        Span(
                            current_span,
                            tag=sorted(
                                tags.items(), key=lambda k_v: k_v[1], reverse=True
                            )[0][0],
                            score=span_score,
                        )
                    )
                current_span = []
                tags = defaultdict(lambda: 0.0)

            if in_span:
                current_span.append(token)
                weight = 1.1 if starts_new_span else 1.0
                tags[tag_value[2:]] += weight

            # remember previous tag
            previous_tag_value = tag_value

        if len(current_span) > 0:
            scores = [t.get_tag(tag_type).score for t in current_span]
            span_score = sum(scores) / len(scores)
            if span_score > min_score:
                spans.append(
                    Span(
                        current_span,
                        tag=sorted(tags.items(), key=lambda k_v: k_v[1], reverse=True)[
                            0
                        ][0],
                        score=span_score,
                    )
                )

        return spans

    def add_label(self, label: Union[Label, str]):
        if type(label) is Label:
            self.labels.append(label)

        elif type(label) is str:
            self.labels.append(Label(label))

    def add_labels(self, labels: Union[List[Label], List[str]]):
        for label in labels:
            self.add_label(label)

    def get_label_names(self) -> List[str]:
        return [label.value for label in self.labels]

    @property
    def embedding(self):
        return self.get_embedding()

    def set_embedding(self, name: str, vector: torch.tensor):
        device = flair.device
        if (flair.embedding_storage_mode == "cpu") and len(self._embeddings.keys()) > 0:
            device = next(iter(self._embeddings.values())).device
        if device != vector.device:
            vector = vector.to(device)
        self._embeddings[name] = vector

    def get_embedding(self) -> torch.tensor:
        embeddings = []
        for embed in sorted(self._embeddings.keys()):
            embedding = self._embeddings[embed]
            embeddings.append(embedding)

        if embeddings:
            return torch.cat(embeddings, dim=0)

        return torch.Tensor()

    def to(self, device: str, pin_memory: bool = False):

        # move sentence embeddings to device
        for name, vector in self._embeddings.items():
            if str(vector.device) != str(device):
                if pin_memory:
                    self._embeddings[name] = vector.to(
                        device, non_blocking=True
                    ).pin_memory()
                else:
                    self._embeddings[name] = vector.to(device, non_blocking=True)

        # move token embeddings to device
        for token in self:
            token.to(device, pin_memory)

    def clear_embeddings(self, embedding_names: List[str] = None):

        # clear sentence embeddings
        if embedding_names is None:
            self._embeddings: Dict = {}
        else:
            for name in embedding_names:
                if name in self._embeddings.keys():
                    del self._embeddings[name]

        # clear token embeddings
        for token in self:
            token.clear_embeddings(embedding_names)

    def to_tagged_string(self, main_tag=None) -> str:
        list = []
        for token in self.tokens:
            list.append(token.text)

            tags: List[str] = []
            for tag_type in token.tags.keys():

                if main_tag is not None and main_tag != tag_type:
                    continue

                if (
                    token.get_tag(tag_type).value == ""
                    or token.get_tag(tag_type).value == "O"
                ):
                    continue
                tags.append(token.get_tag(tag_type).value)
            all_tags = "<" + "/".join(tags) + ">"
            if all_tags != "<>":
                list.append(all_tags)
        return " ".join(list)

    def to_tokenized_string(self) -> str:

        if self.tokenized is None:
            self.tokenized = " ".join([t.text for t in self.tokens])

        return self.tokenized

    def to_plain_string(self):
        plain = ""
        for token in self.tokens:
            plain += token.text
            if token.whitespace_after:
                plain += " "
        return plain.rstrip()

    def convert_tag_scheme(self, tag_type: str = "ner", target_scheme: str = "iob"):

        tags: List[Label] = []
        for token in self.tokens:
            tags.append(token.get_tag(tag_type))

        if target_scheme == "iob":
            iob2(tags)

        if target_scheme == "iobes":
            iob2(tags)
            tags = iob_iobes(tags)

        for index, tag in enumerate(tags):
            self.tokens[index].add_tag(tag_type, tag)

    def infer_space_after(self):
        """
        Heuristics in case you wish to infer whitespace_after values for tokenized text. This is useful for some old NLP
        tasks (such as CoNLL-03 and CoNLL-2000) that provide only tokenized data with no info of original whitespacing.
        :return:
        """
        last_token = None
        quote_count: int = 0
        # infer whitespace after field

        for token in self.tokens:
            if token.text == '"':
                quote_count += 1
                if quote_count % 2 != 0:
                    token.whitespace_after = False
                elif last_token is not None:
                    last_token.whitespace_after = False

            if last_token is not None:

                if token.text in [".", ":", ",", ";", ")", "n't", "!", "?"]:
                    last_token.whitespace_after = False

                if token.text.startswith("'"):
                    last_token.whitespace_after = False

            if token.text in ["("]:
                token.whitespace_after = False

            last_token = token
        return self

    def to_original_text(self) -> str:
        if len(self.tokens) > 0 and (self.tokens[0].start_pos is None):
            return " ".join([t.text for t in self.tokens])
        str = ""
        pos = 0
        for t in self.tokens:
            while t.start_pos != pos:
                str += " "
                pos += 1

            str += t.text
            pos += len(t.text)

        return str

    def to_dict(self, tag_type: str = None):
        labels = []
        entities = []

        if tag_type:
            entities = [span.to_dict() for span in self.get_spans(tag_type)]
        if self.labels:
            labels = [l.to_dict() for l in self.labels]

        return {"text": self.to_original_text(), "labels": labels, "entities": entities}

    def __getitem__(self, idx: int) -> Token:
        return self.tokens[idx]

    def __iter__(self):
        return iter(self.tokens)

    def __repr__(self):
        return 'Sentence: "{}" - {} Tokens'.format(
            " ".join([t.text for t in self.tokens]), len(self)
        )

    def __copy__(self):
        s = Sentence()
        for token in self.tokens:
            nt = Token(token.text)
            for tag_type in token.tags:
                nt.add_tag(
                    tag_type,
                    token.get_tag(tag_type).value,
                    token.get_tag(tag_type).score,
                )

            s.add_token(nt)
        return s

    def __str__(self) -> str:

        if self.labels:
            return f'Sentence: "{self.to_tokenized_string()}" - {len(self)} Tokens - Labels: {self.labels} '
        else:
            return f'Sentence: "{self.to_tokenized_string()}" - {len(self)} Tokens'

    def __len__(self) -> int:
        return len(self.tokens)

    def get_language_code(self) -> str:
        if self.language_code is None:
            import langdetect

            try:
                self.language_code = langdetect.detect(self.to_plain_string())
            except:
                self.language_code = "en"

        return self.language_code


class Image(DataPoint):
    def __init__(self, data=None, imageURL=None):
        self.data = data
        self._embeddings: Dict = {}
        self.imageURL = imageURL

    @property
    def embedding(self):
        return self.get_embedding()

    def __str__(self):

        image_repr = self.data.size() if self.data else ""
        image_url = self.imageURL if self.imageURL else ""

        return f"Image: {image_repr} {image_url}"

    def get_embedding(self) -> torch.tensor:
        embeddings = [
            self._embeddings[embed] for embed in sorted(self._embeddings.keys())
        ]

        if embeddings:
            return torch.cat(embeddings, dim=0)

        return torch.tensor([], device=flair.device)

    def set_embedding(self, name: str, vector: torch.tensor):
        device = flair.device
        if (flair.embedding_storage_mode == "cpu") and len(self._embeddings.keys()) > 0:
            device = next(iter(self._embeddings.values())).device
        if device != vector.device:
            vector = vector.to(device)
        self._embeddings[name] = vector

    def to(self, device: str, pin_memory: bool = False):
        for name, vector in self._embeddings.items():
            if str(vector.device) != str(device):
                if pin_memory:
                    self._embeddings[name] = vector.to(
                        device, non_blocking=True
                    ).pin_memory()
                else:
                    self._embeddings[name] = vector.to(device, non_blocking=True)

    def clear_embeddings(self, embedding_names: List[str] = None):
        if embedding_names is None:
            self._embeddings: Dict = {}
        else:
            for name in embedding_names:
                if name in self._embeddings.keys():
                    del self._embeddings[name]


class FlairDataset(Dataset):
    @abstractmethod
    def is_in_memory(self) -> bool:
        pass


class Corpus:
    def __init__(
        self,
        train: FlairDataset,
        dev: FlairDataset,
        test: FlairDataset,
        name: str = "corpus",
    ):
        self._train: FlairDataset = train
        self._dev: FlairDataset = dev
        self._test: FlairDataset = test
        self.name: str = name

    @property
    def train(self) -> FlairDataset:
        return self._train

    @property
    def dev(self) -> FlairDataset:
        return self._dev

    @property
    def test(self) -> FlairDataset:
        return self._test

    def downsample(self, percentage: float = 0.1, only_downsample_train=False):

        self._train = self._downsample_to_proportion(self.train, percentage)
        if not only_downsample_train:
            self._dev = self._downsample_to_proportion(self.dev, percentage)
            self._test = self._downsample_to_proportion(self.test, percentage)

        return self

    def filter_empty_sentences(self):
        log.info("Filtering empty sentences")
        self._train = Corpus._filter_empty_sentences(self._train)
        self._test = Corpus._filter_empty_sentences(self._test)
        self._dev = Corpus._filter_empty_sentences(self._dev)
        log.info(self)

    @staticmethod
    def _filter_empty_sentences(dataset) -> Dataset:

        # find out empty sentence indices
        empty_sentence_indices = []
        non_empty_sentence_indices = []
        index = 0

        from flair.datasets import DataLoader

        for batch in DataLoader(dataset):
            for sentence in batch:
                if len(sentence) == 0:
                    empty_sentence_indices.append(index)
                else:
                    non_empty_sentence_indices.append(index)
                index += 1

        # create subset of non-empty sentence indices
        subset = Subset(dataset, non_empty_sentence_indices)

        return subset

    def make_vocab_dictionary(self, max_tokens=-1, min_freq=1) -> Dictionary:
        """
        Creates a dictionary of all tokens contained in the corpus.
        By defining `max_tokens` you can set the maximum number of tokens that should be contained in the dictionary.
        If there are more than `max_tokens` tokens in the corpus, the most frequent tokens are added first.
        If `min_freq` is set the a value greater than 1 only tokens occurring more than `min_freq` times are considered
        to be added to the dictionary.
        :param max_tokens: the maximum number of tokens that should be added to the dictionary (-1 = take all tokens)
        :param min_freq: a token needs to occur at least `min_freq` times to be added to the dictionary (-1 = there is no limitation)
        :return: dictionary of tokens
        """
        tokens = self._get_most_common_tokens(max_tokens, min_freq)

        vocab_dictionary: Dictionary = Dictionary()
        for token in tokens:
            vocab_dictionary.add_item(token)

        return vocab_dictionary

    def _get_most_common_tokens(self, max_tokens, min_freq) -> List[str]:
        tokens_and_frequencies = Counter(self._get_all_tokens())
        tokens_and_frequencies = tokens_and_frequencies.most_common()

        tokens = []
        for token, freq in tokens_and_frequencies:
            if (min_freq != -1 and freq < min_freq) or (
                max_tokens != -1 and len(tokens) == max_tokens
            ):
                break
            tokens.append(token)
        return tokens

    def _get_all_tokens(self) -> List[str]:
        tokens = list(map((lambda s: s.tokens), self.train))
        tokens = [token for sublist in tokens for token in sublist]
        return list(map((lambda t: t.text), tokens))

    def _downsample_to_proportion(self, dataset: Dataset, proportion: float):

        sampled_size: int = round(len(dataset) * proportion)
        splits = random_split(dataset, [len(dataset) - sampled_size, sampled_size])
        return splits[1]

    def obtain_statistics(
        self, tag_type: str = None, pretty_print: bool = True
    ) -> dict:
        """
        Print statistics about the class distribution (only labels of sentences are taken into account) and sentence
        sizes.
        """
        json_string = {
            "TRAIN": self._obtain_statistics_for(self.train, "TRAIN", tag_type),
            "TEST": self._obtain_statistics_for(self.test, "TEST", tag_type),
            "DEV": self._obtain_statistics_for(self.dev, "DEV", tag_type),
        }
        if pretty_print:
            import json

            json_string = json.dumps(json_string, indent=4)
        return json_string

    @staticmethod
    def _obtain_statistics_for(sentences, name, tag_type) -> dict:
        if len(sentences) == 0:
            return {}

        classes_to_count = Corpus._get_class_to_count(sentences)
        tags_to_count = Corpus._get_tag_to_count(sentences, tag_type)
        tokens_per_sentence = Corpus._get_tokens_per_sentence(sentences)

        label_size_dict = {}
        for l, c in classes_to_count.items():
            label_size_dict[l] = c

        tag_size_dict = {}
        for l, c in tags_to_count.items():
            tag_size_dict[l] = c

        return {
            "dataset": name,
            "total_number_of_documents": len(sentences),
            "number_of_documents_per_class": label_size_dict,
            "number_of_tokens_per_tag": tag_size_dict,
            "number_of_tokens": {
                "total": sum(tokens_per_sentence),
                "min": min(tokens_per_sentence),
                "max": max(tokens_per_sentence),
                "avg": sum(tokens_per_sentence) / len(sentences),
            },
        }

    @staticmethod
    def _get_tokens_per_sentence(sentences):
        return list(map(lambda x: len(x.tokens), sentences))

    @staticmethod
    def _get_class_to_count(sentences):
        class_to_count = defaultdict(lambda: 0)
        for sent in sentences:
            for label in sent.labels:
                class_to_count[label.value] += 1
        return class_to_count

    @staticmethod
    def _get_tag_to_count(sentences, tag_type):
        tag_to_count = defaultdict(lambda: 0)
        for sent in sentences:
            for word in sent.tokens:
                if tag_type in word.tags:
                    label = word.tags[tag_type]
                    tag_to_count[label.value] += 1
        return tag_to_count

    def __str__(self) -> str:
        return "Corpus: %d train + %d dev + %d test sentences" % (
            len(self.train),
            len(self.dev),
            len(self.test),
        )

    def make_label_dictionary(self) -> Dictionary:
        """
        Creates a dictionary of all labels assigned to the sentences in the corpus.
        :return: dictionary of labels
        """
        label_dictionary: Dictionary = Dictionary(add_unk=False)
        label_dictionary.multi_label = False

        from flair.datasets import DataLoader

        loader = DataLoader(self.train, batch_size=1)

        log.info("Computing label dictionary. Progress:")
        for batch in Tqdm.tqdm(iter(loader)):

            for sentence in batch:

                for label in sentence.labels:
                    label_dictionary.add_item(label.value)

                if not label_dictionary.multi_label:
                    if len(sentence.labels) > 1:
                        label_dictionary.multi_label = True

        log.info(label_dictionary.idx2item)

        return label_dictionary

    def get_label_distribution(self):
        class_to_count = defaultdict(lambda: 0)
        for sent in self.train:
            for label in sent.labels:
                class_to_count[label.value] += 1
        return class_to_count

    def get_all_sentences(self) -> Dataset:
        return ConcatDataset([self.train, self.dev, self.test])

    def make_tag_dictionary(self, tag_type: str) -> Dictionary:

        # Make the tag dictionary
        tag_dictionary: Dictionary = Dictionary()
        tag_dictionary.add_item("O")
        for sentence in self.get_all_sentences():
            for token in sentence.tokens:
                tag_dictionary.add_item(token.get_tag(tag_type).value)
        tag_dictionary.add_item("<START>")
        tag_dictionary.add_item("<STOP>")
        return tag_dictionary


class MultiCorpus(Corpus):
    def __init__(self, corpora: List[Corpus], name: str = "multicorpus"):
        self.corpora: List[Corpus] = corpora

        super(MultiCorpus, self).__init__(
            ConcatDataset([corpus.train for corpus in self.corpora]),
            ConcatDataset([corpus.dev for corpus in self.corpora]),
            ConcatDataset([corpus.test for corpus in self.corpora]),
            name=name,
        )

    def __str__(self):
        return "\n".join([str(corpus) for corpus in self.corpora])


def iob2(tags):
    """
    Check that tags have a valid IOB format.
    Tags in IOB1 format are converted to IOB2.
    """
    for i, tag in enumerate(tags):
        if tag.value == "O":
            continue
        split = tag.value.split("-")
        if len(split) != 2 or split[0] not in ["I", "B"]:
            return False
        if split[0] == "B":
            continue
        elif i == 0 or tags[i - 1].value == "O":  # conversion IOB1 to IOB2
            tags[i].value = "B" + tag.value[1:]
        elif tags[i - 1].value[1:] == tag.value[1:]:
            continue
        else:  # conversion IOB1 to IOB2
            tags[i].value = "B" + tag.value[1:]
    return True


def iob_iobes(tags):
    """
    IOB -> IOBES
    """
    new_tags = []
    for i, tag in enumerate(tags):
        if tag.value == "O":
            new_tags.append(tag.value)
        elif tag.value.split("-")[0] == "B":
            if i + 1 != len(tags) and tags[i + 1].value.split("-")[0] == "I":
                new_tags.append(tag.value)
            else:
                new_tags.append(tag.value.replace("B-", "S-"))
        elif tag.value.split("-")[0] == "I":
            if i + 1 < len(tags) and tags[i + 1].value.split("-")[0] == "I":
                new_tags.append(tag.value)
            else:
                new_tags.append(tag.value.replace("I-", "E-"))
        else:
            raise Exception("Invalid IOB format!")
    return new_tags
