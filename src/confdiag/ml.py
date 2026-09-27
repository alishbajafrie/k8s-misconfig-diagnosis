"""Logistic regression over TF-IDF text features and numeric state."""
from __future__ import annotations

import numpy as np
from scipy import sparse
from sklearn.feature_extraction import DictVectorizer
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression

from .features import ABLATIONS

TEXT_GROUPS = {"logs_text", "k8s_text"}
NUM_GROUPS = {"symptom", "k8s_state"}


class Diagnoser:
    def __init__(self, ablation: str = "logs+k8s", C: float = 1.0, seed: int = 0):
        self.groups = ABLATIONS[ablation]
        self.C = C
        self.seed = seed

    def _text_vectorizer(self) -> TfidfVectorizer:
        return TfidfVectorizer(token_pattern=r"<\w+>|[a-z_]{2,}", ngram_range=(1, 2),
                               min_df=1, sublinear_tf=True)

    def _matrix(self, feats: list[dict], fit: bool) -> sparse.csr_matrix:
        blocks = []
        for g in self.groups:
            if g in TEXT_GROUPS:
                docs = [f[g] for f in feats]
                if fit:
                    self.vec_[g] = self._text_vectorizer()
                    blocks.append(self.vec_[g].fit_transform(docs))
                else:
                    blocks.append(self.vec_[g].transform(docs))
            elif g in NUM_GROUPS:
                rows = [{k: float(np.log1p(max(v, 0.0))) for k, v in f[g].items()} for f in feats]
                if fit:
                    self.vec_[g] = DictVectorizer()
                    blocks.append(self.vec_[g].fit_transform(rows))
                else:
                    blocks.append(self.vec_[g].transform(rows))
        return sparse.hstack(blocks).tocsr()

    def fit(self, feats: list[dict], labels: list[str]) -> "Diagnoser":
        self.vec_: dict = {}
        X = self._matrix(feats, fit=True)
        self.clf_ = LogisticRegression(C=self.C, max_iter=5000, class_weight="balanced",
                                       random_state=self.seed)
        self.clf_.fit(X, labels)
        return self

    def predict(self, feats: list[dict]) -> list[str]:
        return list(self.clf_.predict(self._matrix(feats, fit=False)))

    def top_features(self, k: int = 8) -> dict[str, list[str]]:
        names = []
        for g in self.groups:
            names.extend(f"{g}:{n}" for n in self.vec_[g].get_feature_names_out())
        out = {}
        coefs = self.clf_.coef_
        classes = self.clf_.classes_
        if len(classes) == 2:
            coefs = np.vstack([-coefs[0], coefs[0]])
        for cls, row in zip(classes, coefs):
            idx = np.argsort(row)[::-1][:k]
            out[str(cls)] = [names[i] for i in idx]
        return out
