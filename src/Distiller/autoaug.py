from nlpaug.augmenter import char as nac
from nlpaug.augmenter import word as naw
from nlpaug import flow as naf
from nlpaug.util.audio.loader import AudioLoader
from nlpaug.util import Action
import random
import numpy as np
import json

class AutoAugmenter:
    def __init__(self, aug_args):
        augmenter_table = {"contextual": naw.ContextualWordEmbsAug,
                           "random": naw.RandomWordAug,
                           "back_translation": naw.BackTranslationAug}
        self.augs = []
        self.aug_names = []
        for i in aug_args:
            if i:
                name = i.pop("aug_type")
                print(f"Load Augmenter {name}")
                self.aug_names.append(name)
                self.augs.append(augmenter_table.get(name)(**i))
        # self.aug = augmenter_table.get(aug_type)(**aug_args)

    @classmethod
    def from_config(cls, aug_type):
        augmenter_config_path = f"{aug_type}_augmenter_config.json"
        with open(augmenter_config_path) as f:
            aug_args = json.load(f)
        aug_args["aug_type"] = aug_type
        return cls(aug_args)

    @classmethod
    def init_pipeline(cls, w=None):
        config_list = [{
          "aug_type": "contextual",
          "model_type": "distilbert",
          "top_k": 100,
          "aug_min": 10,
          "aug_max": 25,
          "aug_p": 0.5,
          "device": "cpu"
            },{
            "aug_type": "back_translation",
            "from_model_name": "transformer.wmt18.en-de",
            "from_model_checkpt": "wmt18.model1.pt",
            "to_model_name": "transformer.wmt19.de-en",
            "to_model_checkpt": "model1.pt"
        },{
        "aug_type": "random",
        "action": "swap",
        "aug_min": 3,
        "aug_max": 10
    }]
        selected_list = []
        aug_args = []
        if w:
            for i,d in enumerate(w):
                if d != 0:
                    aug_args.append(config_list[i])
        else:
            for i in range(3):
                while True:
                    aug_config = random.choice(config_list)
                    if aug_config['aug_type'] not in selected_list:
                        selected_list.append(aug_config['aug_type'])
                        break
                if random.random()>0.5:
                    aug_args.append(aug_config)
        return cls(aug_args)


    def augment(self, data):
        # result = []
        for aug in self.augs:
            data = aug.augment(data)
        return data
        # return self.aug.augment(data)

    def __len__(self):
        return len(self.augs)
        # return 1