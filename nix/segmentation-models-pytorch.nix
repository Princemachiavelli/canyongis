{
  lib,
  buildPythonPackage,
  fetchFromGitHub,
  setuptools,
  huggingface-hub,
  numpy,
  pillow,
  safetensors,
  timm,
  torch,
  torchvision,
  tqdm,
}:

buildPythonPackage rec {
  pname = "segmentation-models-pytorch";
  version = "0.5.0";
  pyproject = true;

  src = fetchFromGitHub {
    owner = "qubvel-org";
    repo = "segmentation_models.pytorch";
    tag = "v${version}";
    hash = "sha256-QtrmMbVcFHftV69stJHk0+3n1o0inlO22xHs/smLlGg=";
  };

  build-system = [ setuptools ];

  dependencies = [
    huggingface-hub
    numpy
    pillow
    safetensors
    timm
    torch
    torchvision
    tqdm
  ];

  doCheck = false;
  pythonImportsCheck = [ "segmentation_models_pytorch" ];

  meta = {
    description = "Image segmentation models with pre-trained backbones for PyTorch";
    homepage = "https://github.com/qubvel-org/segmentation_models.pytorch";
    changelog = "https://github.com/qubvel-org/segmentation_models.pytorch/releases/tag/v${version}";
    license = lib.licenses.mit;
  };
}
