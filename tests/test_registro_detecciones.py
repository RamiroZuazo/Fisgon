"""
Pruebas del registro de detecciones (inference_worker), usado en la
validación funcional: registra también lo que queda por debajo del umbral,
sin cambiar lo que dispara una alerta.
"""

import csv

from inference.inference_worker import InferenceWorker


class Valor:
    """Imita un tensor de un elemento: valor[0] devuelve el número."""

    def __init__(self, v):
        self.v = v

    def __getitem__(self, _):
        return self.v


class Caja:
    def __init__(self, conf, cls=0):
        self.conf = Valor(conf)
        self.cls = Valor(cls)
        self.xyxy = [Caja.Tensor([10.0, 20.0, 40.0, 60.0])]

    class Tensor(list):
        def tolist(self):
            return list(self)


class Resultado:
    names = {0: "gun", 1: "knife"}

    def __init__(self, confs):
        self.boxes = [Caja(c) for c in confs]


class ModeloFalso:
    def __init__(self, confs):
        self.confs = confs
        self.conf_pedida = None

    def predict(self, source, conf, iou, verbose):
        self.conf_pedida = conf
        return [Resultado([c for c in self.confs if c >= conf]) for _ in source]


def _worker(tmp_path, confs, registro=True):
    detecciones = []
    config = {"inference": {"confidence": 0.7}}
    if registro:
        config["registro_detecciones"] = {"enabled": True, "min_confidence": 0.2,
                                          "archivo": str(tmp_path / "detecciones.csv")}
    modelo = ModeloFalso(confs)
    worker = InferenceWorker(frame_queue=None, modelo=modelo, config=config, on_detection=detecciones.append)
    worker._procesar_lote([{"camera_id": "cam_1", "frame": object(), "timestamp": 100.0}])
    return modelo, detecciones


def test_registra_lo_que_queda_por_debajo_del_umbral_sin_alertar(tmp_path, frame):
    modelo, detecciones = _worker(tmp_path, [0.3, 0.8])

    assert modelo.conf_pedida == 0.2  # el modelo se llama con el umbral del registro
    assert [d["confidence"] for d in detecciones] == [0.8]  # solo alerta lo que supera 0,7
    filas = list(csv.DictReader(open(tmp_path / "detecciones.csv", encoding="utf-8")))
    assert sorted(float(f["confidence"]) for f in filas) == [0.3, 0.8]
    assert {f["object_class"] for f in filas} == {"pistol"}


def test_sin_registro_el_modelo_usa_el_umbral_del_sistema(tmp_path):
    modelo, detecciones = _worker(tmp_path, [0.3, 0.8], registro=False)

    assert modelo.conf_pedida == 0.7
    assert [d["confidence"] for d in detecciones] == [0.8]
    assert not (tmp_path / "detecciones.csv").exists()
