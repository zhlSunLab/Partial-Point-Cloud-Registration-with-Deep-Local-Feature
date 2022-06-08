# Partial Point Cloud Registration with Deep Local Feature
## Prerequisites
The code has been tested running under Python 3.6.10.
The required packages are as follows:
- Pytorch=1.0.1
- numpy
- scikit-learn
- h5py
- tqdm
## Code Reference
Code reference: Partial Registration Network: [GitHub - WangYueFt/prnet: prnet](https://github.com/WangYueFt/prnet)
## Training
### exp1 modelnet40 unseen objects
```shell
python main.py --exp_name=exp1
```
### exp2 modelnet40 unseen categories
```shell
python main.py --exp_name=exp2 --unseen=True
```
### exp3 modelnet40 unseen objects with Gaussian noise
```shell
python main.py --exp_name=exp3 --gaussian_noise=True
```
