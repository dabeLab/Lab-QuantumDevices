import os
import glob
import random
import numpy as np
import torch
import nibabel as nib
import tqdm
import datetime
import pickle
import pydicom
from monai.networks.nets import UNet, UNETR
from monai.transforms import Compose, LoadImaged, EnsureChannelFirstd, Spacingd, OrientationD, ScaleIntensityRanged, \
    AsDiscreted, AsDiscrete, SpacingD
from monai.data import CacheDataset, DataLoader, decollate_batch
from monai.inferers import sliding_window_inference
from monai.losses import DiceLoss
from monai.metrics import DiceMetric
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
from skimage.color import label2rgb
from skimage.measure import label, regionprops
from skimage.exposure import rescale_intensity
from skimage.morphology import ball, remove_small_objects, binary_opening, binary_closing


class DeepLearningExperiment:
    """This class is a wrapper around MONAI. It handles dataset preparation, model training, validating
    and testing. It includes libraries for plotting (under development), and statistical analysis (in dev.). """
    def __init__(self):
        # The class assumes by default data are stored in main folder -> [data, dataset, model,...]
        # however these subdirectory can be changed by changing the corresponding attribute
        # Note: main -> dataset includes training, validation and testing datasets
        self.path_main = None
        self.path_dataset = "dataset"
        self.path_data_raw = "raw"
        self.path_model = "model"
        self.path_results = "results"
        self.experiment = None  # experiment name. Generated by the generate_experiment_name method launched
        # at the beginning of the training phase.
        # MODEL
        self.model = None
        self.model_info = None
        self.n_classes = 2
        self.optimizer = None
        self.max_iteration_trn = 100  # max iteration for training
        self.delta_iteration_trn = 1  # number of iterations in-between each validation step
        # TRANSFORMATIONS
        self.intensity_min = -1.0  # min voxel intensity value, used for intensity rescaling
        self.intensity_max = 1.0  # max voxel intensity value, used for intensity rescaling
        self.transforms_trn = None
        self.transforms_val = None
        self.transforms_tst = None
        # IMAGE METADATA
        self.voxel = (1.0, 1.0, 1.0)  # voxel dimensions (dx, dy, dz) in mm
        # DATA AND LOADERS
        self.data_percentage = 1
        self.dataset_trn_ratio = 0.7  # percentage of data used for training
        self.dataset_val_ratio = 0.2  # percentage of data used for validating
        self.dataset_tst_ratio = 0.1  # percentage of data used for testing
        self.batch_trn_size = 1  # training dataset batch size
        self.batch_val_size = 1  # validation dataset batch size
        self.batch_tst_size = 1  # testing dataset batch size
        self.dataset_trn = {}  # list of dictionary for training the model (compiled by method)
        self.dataset_val = {}  # list of dictionary for validating the model (compiled by method)
        self.dataset_tst = {}  # list of dictionary for testing the model (compiled by method)
        self.loader_trn = None  # training data loader (compiled by method)
        self.loader_val = None  # validation data loader (compiled by method)
        self.loader_tst = None  # testing data loader (compiled by method)
        # MODEL PERFORMANCE INDEXES
        self.loss_function = None
        self.metric_function = None
        self.epochs = None
        self.losses = None
        self.scores = None
        # HARDWARE
        self.device = torch.device(self.cuda_mps_cpu())
        self.check_gpu()

    @staticmethod
    def cuda_mps_cpu():
        if torch.cuda.is_available():
            return "cuda"
        elif torch.backends.mps.is_available() and torch.backends.mps.is_built() and False:
            return "mps"
        else:
            return "cpu"

    # extract dx, dy, and dz from a representative sample
    def get_voxel_size(self):
        if not self.dataset_trn:
            print("Error: dataset_trn is empty. Build datasets first.")
            return
        self.voxel[0], self.voxel[1], self.voxel[2] = nib.load(self.dataset_trn[0]["img"]).header.get_zooms()

    # save model
    def save_model(self):
        torch.save(self.model, os.path.join(self.path_model, self.experiment))

    # load model
    def load_model(self, model):
        self.model = torch.load(os.path.join(self.path_main, self.path_model, model), map_location=self.device)

    # save all attributes but model
    def save_attributes(self, filename, *attributes):
        data_to_save = {f"{attr}": getattr(self, attr) for attr in attributes}
        with open(filename, 'wb') as file:
            pickle.dump(data_to_save, file)

    # load all attributes but model
    def load_attributes(self, filename):
        with open(filename, 'rb') as file:
            loaded_data = pickle.load(file)
        for key, val in loaded_data.items():
            setattr(self, key, val)

    # define the transformation for the training dataset
    def compose_transforms_trn(self):
        self.transforms_trn = Compose([
            LoadImaged(
                keys=["img", "lbl"]),
            EnsureChannelFirstd(
                keys=["img", "lbl"]),
            # AsDiscreted(
            #     keys=["lbl"], to_onehot=self.n_classes),
            Spacingd(
                keys=["img", "lbl"],
                pixdim=(self.voxel[0], self.voxel[1], self.voxel[2]),
                mode=("bilinear", "nearest")),
            OrientationD(
                keys=["img", "lbl"],
                axcodes="RAS"),
            # rescale intensity in the range [0, 1]
            ScaleIntensityRanged(
                keys=["img"],
                a_min=self.intensity_min,
                a_max=self.intensity_max,
                b_min=0.0,
                b_max=1.0,
                clip=True),
        ])

    # define the transformation for the validation dataset
    def compose_transforms_val(self):
        self.transforms_val = Compose([
            LoadImaged(keys=["img", "lbl"]),
            EnsureChannelFirstd(keys=["img", "lbl"]),
            AsDiscreted(keys=["lbl"], to_onehot=self.n_classes),
            Spacingd(keys=["img", "lbl"], pixdim=(self.voxel[0], self.voxel[1], self.voxel[2]),
                     mode=("bilinear", "nearest")),
            OrientationD(keys=["img", "lbl"], axcodes="RAS"),
            ScaleIntensityRanged(keys=["img"], a_min=self.intensity_min, a_max=self.intensity_max,
                                 b_min=0.0, b_max=1.0, clip=True),
        ])

    # define the transformation for the testing dataset
    def compose_transforms_tst(self):
        self.transforms_tst = Compose([
            LoadImaged(keys=["img", "lbl"]),
            EnsureChannelFirstd(keys=["img", "lbl"]),
            AsDiscreted(keys=["lbl"], to_onehot=self.n_classes),
            Spacingd(keys=["img", "lbl"], pixdim=(self.voxel[0], self.voxel[1], self.voxel[2]),
                     mode=("bilinear", "nearest")),
            OrientationD(keys=["img", "lbl"], axcodes="RAS"),
            ScaleIntensityRanged(keys=["img"], a_min=self.intensity_min, a_max=self.intensity_max,
                                 b_min=0.0, b_max=1.0, clip=True),
        ])

    # build datasets. This function will not cache the data, just build a list of dictionaries for trn, val, and tst
    def build_dataset(self):
        # Each sample is a dictionary with (image path, labels path), and no actual data
        path_images = sorted(glob.glob(os.path.join("dataset", "img*.nii.gz")))
        path_labels = sorted(glob.glob(os.path.join("dataset", "sgm*.nii.gz")))
        path_dicts = [{"img": image_name, "lbl": label_name} for image_name, label_name in zip(path_images, path_labels)]
        # select subset of data
        if self.data_percentage < 1:
            path_dicts = path_dicts[:int(len(path_dicts) * self.data_percentage)]
        # Calculate the number of samples for each split
        n = len(path_dicts)
        n_tra = int(self.dataset_trn_ratio * n)
        n_val = int(self.dataset_val_ratio * n)
        # Shuffle the data list to randomize the order
        random.shuffle(path_dicts)
        # Split the data into training, validation, and testing sets, and store paths in attributes
        self.dataset_trn = path_dicts[:n_tra]
        self.dataset_val = path_dicts[n_tra:n_tra + n_val]
        self.dataset_tst = path_dicts[n_tra + n_val:]

    # cache training dataset and generate loader
    def cache_dataset_trn(self):
        if self.dataset_trn:
            dataset_trn = CacheDataset(data=self.dataset_trn, transform=self.transforms_trn)
            self.loader_trn = DataLoader(dataset_trn, batch_size=self.batch_trn_size)

    # cache validation dataset and generate loader
    def cache_dataset_val(self):
        if self.dataset_val:
            dataset_val = CacheDataset(data=self.dataset_val, transform=self.transforms_val)
            self.loader_val = DataLoader(dataset_val, batch_size=self.batch_val_size)

    # cache testing dataset and generate loader
    def cache_dataset_tst(self):
        if self.dataset_tst:
            dataset_tst = CacheDataset(data=self.dataset_tst, transform=self.transforms_tst)
            self.loader_tst = DataLoader(dataset_tst, batch_size=self.batch_tst_size)

    # build a UNet model
    def build_model_unet(self,
                         kernel_size=3,
                         up_kernel_size=3,
                         channels=(64, 128, 256, 512, 1024),
                         strides=(2, 2, 2, 2),
                         activation_function=torch.nn.ReLU(),
                         dropout_ratio=0,
                         learning_rate=1e-3,  # learning rate of the Adam optimizer
                         weight_decay=1e-4,
                         num_res_units=1):  # weight decay of the Adam optimizer
        model = UNet(
            spatial_dims=3,
            in_channels=1,
            out_channels=self.n_classes,
            channels=channels,  # sequence of channels. Top block first. len(channels) >= 2
            strides=strides,  # sequence of convolution strides. len(strides) = len(channels) - 1.
            kernel_size=kernel_size,
            # convolution kernel size, the value(s) should be odd. If sequence, its length should equal
            # to dimensions. Defaults to 3
            up_kernel_size=up_kernel_size,
            # Upsampling convolution kernel size, the value(s) should be odd. If sequence, its length
            # should equal to dimensions. Defaults to 3
            num_res_units=num_res_units,  # number of residual units. Defaults to 0.
            #  act=params["activation_function"],
            dropout=dropout_ratio
        ).to(self.device)
        self.model = model
        self.loss_function = DiceLoss(softmax=True)
        self.optimizer = torch.optim.AdamW(self.model.parameters(), lr=learning_rate, weight_decay=weight_decay)
        self.dice_metric = DiceMetric(include_background=False, reduction="mean")
        self.model_info = {"model": "unet",
                           "channels": channels,
                           "strides": strides,
                           "num_res_units": num_res_units}

    # build a UNETR model
    def build_model_unetr(self,
                          num_heads = 12,
                          feature_size = 16,
                          patch_size=(128, 128, 128),
                          learning_rate=1e-3,
                          weight_decay=1e-4):
        model = UNETR(
            in_channels=1,
            out_channels=self.n_classes,
            img_size=patch_size,  # dimension of input (patch) image.
            feature_size=feature_size,  # dimension of network feature size. Defaults to 16.
            hidden_size=768,  # dimension of hidden layer. Defaults to 768.
            mlp_dim=3072,  # dimension of feedforward layer. Defaults to 3072.
            num_heads=num_heads,  # number of attention heads. Defaults to 12.
            pos_embed="perceptron",
            norm_name="instance",
            res_block=True,
            dropout_rate=0.0,
        ).to(self.device)
        self.model = model
        self.loss_function = DiceLoss(softmax=True)
        self.optimizer = torch.optim.AdamW(self.model.parameters(), lr=learning_rate, weight_decay=weight_decay)
        self.dice_metric = DiceMetric(include_background=False, reduction="mean")
        self.model_info = {"model": "unetr",
                           "attention heads": num_heads,
                           "feature size": feature_size}

    # check gpu availability and print to screen
    def check_gpu(self):
        print(f"Model running on {self.device}")

    def generate_experiment_name(self):
        self.experiment = (f"{datetime.datetime.now().strftime('%Y.%m.%d %H.%M.%S')} "
                           ', '.join(f'{key}={value}' for key, value in self.model_info.items()))

    # train model
    def train(self):
        # generate the experiment name
        self.generate_experiment_name()
        # generate arrays for training and validating plotting purposes
        self.epochs = np.arange(self.max_iteration_trn)
        self.losses = np.zeros(self.max_iteration_trn)
        self.metrics = np.zeros(self.max_iteration_trn)
        for epoch in range(self.max_iteration_trn):
            # set the model to training. This has effect only on some transforms
            self.model.train()
            # initialize the epoch's loss
            epoch_loss = 0
            epoch_trn_iterator = tqdm.tqdm(self.loader_trn, desc="Training (X / X Steps) (loss=X.X)", dynamic_ncols=True, miniters=1)
            for step_trn, batch_trn in enumerate(epoch_trn_iterator):
                # reset the optimizer (which stores the values from the previous iteration
                self.optimizer.zero_grad()
                # send the training data to device (GPU)
                inputs, targets = batch_trn['img'].to(self.device), batch_trn['lbl'].to(self.device)
                # forward pass
                outputs = self.model(inputs)
                # calculate loss and add it to epoch's loss
                loss = self.loss_function(outputs, targets)
                epoch_loss += loss.item()
                # backpropagation
                loss.backward()
                # update metrics
                self.optimizer.step()
                # Update the progress bar description with loss and metrics
                epoch_trn_iterator.set_description(f"Training ({epoch + 1} / {self.max_iteration_trn} Steps) (loss={epoch_loss:2.5f})")
            # store epoch's loss in losses array
            self.losses[epoch] = epoch_loss
            # validate model every "delta_iteration"
            if epoch == 0 or (epoch + 1) % self.delta_iteration_trn == 0 or (epoch + 1) == self.max_iteration_trn:
                # run validation
                score = self.validate(epoch)
                # store validation metrics in metrics array
                self.scores[epoch] = score
                # save model to disc
                torch.save(self.model, os.path.join(self.path_model, f"{self.experiment} - iter {epoch + 1:03d} mdl.pth"))

    # validate model
    def validate(self, epoch):
        # set validation metric to zero
        epoch_score = 0
        # set the model to validation. This affects some transforms.
        self.model.eval()
        # disable gradient computation (which is useless for validation)
        with torch.no_grad():
            epoch_val_iterator = tqdm.tqdm(self.loader_val, desc="Validate (X / X Steps) (dice=X.X)", dynamic_ncols=True, miniters=1)
            for step_val, batch_val in enumerate(epoch_val_iterator):
                # reset dice metrics for next validation round.
                # ???? Do we do this in between batches ???
                self.dice_metric.reset()
                # send the validation data to device (GPU)
                img_val, lbl_val = batch_val["img"].to(self.device), batch_val["lbl"].to(self.device)
                # run inference by forward passing the input data through the model
                prd_val = self.model(img_val)
                # binarize the prediction (as required by the dice metric)
                prd_val = AsDiscrete(threshold=0.5)(prd_val)
                self.dice_metric(y_pred=prd_val, y=lbl_val)
                # evaluate metric. Need to aggregate first and then collect the item.
                batch_score = self.dice_metric.aggregate().item()
                # add batch's validation metric and then calculate average metric
                epoch_score += batch_score
                score_mean = epoch_score / (step_val + 1)
                # Update the progress bar description with metric
                epoch_val_iterator.set_description(f"Validate ({epoch + 1} / {self.max_iteration_trn} Steps) (dice={score_mean:2.5f})")
        return score_mean

    # plot training loss vs epoch
    def plot_loss(self, show=False):
        plt.figure()
        plt.plot(self.epochs, self.losses, lw=0, ms=6, marker='o', color='black')
        plt.title("Training Loss")
        plt.xlabel("Epoch")
        plt.ylabel("Loss")
        plt.tight_layout()
        if show:
            plt.show()
        plt.savefig(os.path.join(self.path_model, f"{self.experiment} loss vs epoch.png"))

    # plot validation metric vs epoch
    def plot_metr(self, show=False):
        plt.figure()
        plt.plot(self.epochs[self.metrics != 0], self.metrics[self.metrics != 0], lw=0, ms=6, marker='o', color='black')
        plt.title("Validation Metric")
        plt.xlabel("Epoch")
        plt.ylabel("Metric")
        plt.tight_layout()
        if show:
            plt.show()
        plt.savefig(os.path.join(self.path_model, f"{self.experiment} metric vs epoch.png"))

    # plot segmentation
    def testing(self, morph):
        epoch_tst_iterator = tqdm.tqdm(self.loader_tst, desc="Validating (X / X Steps)", dynamic_ncols=True, miniters=1)
        for step_tst, batch_tst in enumerate(epoch_tst_iterator):
            epoch_tst_iterator.set_description(f"Validating ({step_tst + 1} / {len(epoch_tst_iterator)} Steps)")
            img_tst, lbl_tst = batch_tst["img"].to(self.device), batch_tst["lbl"].to(self.device)
            lbl_prd = self.model(img_tst)
            # convert tensors from one hot encoding to single channel
            img_tst = img_tst[0, 0, :, :, :].cpu().numpy()
            lbl_tst = torch.argmax(lbl_tst, dim=1)[0, :, :, :].cpu().numpy()
            lbl_prd = torch.argmax(lbl_prd, dim=1)[0, :, :, :].cpu().numpy()
            # morphological operation: erosion
            if morph is True:
                for n in range(2):
                    lbl_prd = binary_opening(lbl_prd, ball(2))
                lbl_prd = binary_closing(lbl_prd, ball(1))
                lbl_prd = remove_small_objects(lbl_prd, 64)
            print(f"\nPlotting sample {step_tst}...")
            # Find the (x,y,z) coordinates of the sample center
            x = int(np.floor(img_tst.shape[0] / 2))
            y = int(np.floor(img_tst.shape[1] / 2))
            z = int(np.floor(img_tst.shape[2] / 2))
            # run a connectivity analysis on test and prediction
            lbl_tst = label(lbl_tst, connectivity=1)
            rgn_tst = regionprops(lbl_tst)
            n_rgn_tst = len(rgn_tst)
            lbl_prd = label(lbl_prd, connectivity=1)
            rgn_prd = regionprops(lbl_prd)
            n_rgn_prd = len(rgn_prd)
            print(f"N. regions ground truth: {n_rgn_tst}\n"
                  f"N. regions prediction: {n_rgn_prd}\n"
                  f"Relative error (%): {(n_rgn_prd - n_rgn_tst) / n_rgn_tst * 100}")
            # Generate overlay images
            img_tst_x = img_tst[x, :, :]
            img_tst_y = img_tst[:, y, :]
            img_tst_z = img_tst[:, :, z]
            lbl_tst_x = lbl_tst[x, :, :]
            lbl_tst_y = lbl_tst[:, y, :]
            lbl_tst_z = lbl_tst[:, :, z]
            lbl_prd_x = lbl_prd[x, :, :]
            lbl_prd_y = lbl_prd[:, y, :]
            lbl_prd_z = lbl_prd[:, :, z]
            img_label_overlay_x_tst = label2rgb(label=lbl_tst_x, image=rescale_intensity(img_tst_x, out_range=(0, 1)),
                                                alpha=0.3, bg_label=0, bg_color=None, kind="overlay", saturation=0.6)
            img_label_overlay_y_tst = label2rgb(label=lbl_tst_y, image=rescale_intensity(img_tst_y, out_range=(0, 1)),
                                                alpha=0.3, bg_label=0, bg_color=None, kind="overlay", saturation=0.6)
            img_label_overlay_z_tst = label2rgb(label=lbl_tst_z, image=rescale_intensity(img_tst_z, out_range=(0, 1)),
                                                alpha=0.3, bg_label=0, bg_color=None, kind="overlay", saturation=0.6)
            img_label_overlay_x_prd = label2rgb(label=lbl_prd_x, image=rescale_intensity(img_tst_x, out_range=(0, 1)),
                                                alpha=0.3, bg_label=0, bg_color=None, kind="overlay", saturation=0.6)
            img_label_overlay_y_prd = label2rgb(label=lbl_prd_y, image=rescale_intensity(img_tst_y, out_range=(0, 1)),
                                                alpha=0.3, bg_label=0, bg_color=None, kind="overlay", saturation=0.6)
            img_label_overlay_z_prd = label2rgb(label=lbl_prd_z, image=rescale_intensity(img_tst_z, out_range=(0, 1)),
                                                alpha=0.3, bg_label=0, bg_color=None, kind="overlay", saturation=0.6)
            # plot
            fig, axs = plt.subplots(2, ncols=3)
            axs[0, 0].set_title(f"YZ plane at X = {x} px")
            axs[0, 0].imshow(img_label_overlay_x_tst)
            axs[1, 0].imshow(img_label_overlay_x_prd)
            axs[0, 1].set_title(f"XZ plane at Y = {y} px")
            axs[0, 1].imshow(img_label_overlay_y_tst)
            axs[1, 1].imshow(img_label_overlay_y_prd)
            axs[0, 2].set_title(f"XY plane at Z = {z} px")
            axs[0, 2].imshow(img_label_overlay_z_tst)
            axs[1, 2].imshow(img_label_overlay_z_prd)
            # Remove x-axis and y-axis ticks, labels, and tick marks for all subplots
            for ax in axs.flat:
                ax.set_xticks([])
                ax.set_yticks([])
                ax.set_xticklabels([])
                ax.set_yticklabels([])
            # Adjust layout for better spacing
            plt.tight_layout()
            # Save the figure to a file
            plt.savefig(os.path.join("test", f"{self.params['experiment']} img {step_tst:02d} xyz {x, y, z}.png"),
                        dpi=1200)
            # Add rectangles around regions
            regions = regionprops(lbl_tst_x)
            for region in regions:
                minr, minc, maxr, maxc = region.bbox
                rect = Rectangle((minc, minr), maxc - minc, maxr - minr, fill=False, edgecolor='red', linewidth=0.3)
                axs[0, 0].add_patch(rect)
            regions = regionprops(lbl_tst_y)
            for region in regions:
                minr, minc, maxr, maxc = region.bbox
                rect = Rectangle((minc, minr), maxc - minc, maxr - minr, fill=False, edgecolor='red', linewidth=0.3)
                axs[0, 1].add_patch(rect)
            regions = regionprops(lbl_tst_z)
            for region in regions:
                minr, minc, maxr, maxc = region.bbox
                rect = Rectangle((minc, minr), maxc - minc, maxr - minr, fill=False, edgecolor='red', linewidth=0.3)
                axs[0, 2].add_patch(rect)
            regions = regionprops(lbl_prd_x)
            for region in regions:
                minr, minc, maxr, maxc = region.bbox
                rect = Rectangle((minc, minr), maxc - minc, maxr - minr, fill=False, edgecolor='red', linewidth=0.3)
                axs[1, 0].add_patch(rect)
            regions = regionprops(lbl_prd_y)
            for region in regions:
                minr, minc, maxr, maxc = region.bbox
                rect = Rectangle((minc, minr), maxc - minc, maxr - minr, fill=False, edgecolor='red', linewidth=0.3)
                axs[1, 1].add_patch(rect)
            regions = regionprops(lbl_prd_z)
            for region in regions:
                minr, minc, maxr, maxc = region.bbox
                rect = Rectangle((minc, minr), maxc - minc, maxr - minr, fill=False, edgecolor='red', linewidth=0.3)
                axs[1, 2].add_patch(rect)
            # Save the figure to a file
            plt.savefig(os.path.join("test", f"{self.params['experiment']} img {step_tst:02d} xyz {x, y, z} box.png"),
                        dpi=1200)
            plt.close()

    # load a large ct scan and segment
    def segment(self, path_img, path_lbl):
        path_dict = [{"image": path_img, "label": path_lbl}]
        # Define transforms for loading and preprocessing the NIfTI files
        transforms = Compose([
            # CropLabelledVolumed(keys=["image", "label"]),
            LoadImaged(keys=["image", "label"]),
            EnsureChannelFirstd(keys=["image", "label"]),
            # Spacingd(keys=["image", "label"], pixdim=(dx, dy, dz), mode=("bilinear", "nearest")),
            OrientationD(keys=["image", "label"], axcodes="RAS"),
            ScaleIntensityRanged(keys=["image"], a_min=self.params['intensity_min'], a_max=self.params['intensity_max'],
                                 b_min=0.0, b_max=1.0, clip=True),
        ])
        dataset = CacheDataset(data=path_dict, transform=transforms)
        loader = DataLoader(dataset)

        for step_val, batch_val in enumerate(loader):
            img_inp, lbl_inp = batch_val["img"].to(self.device), batch_val["lbl"].to(self.device)
            # run inference window
            roi_size = (128, 128, 128)
            sw_batch_size = 4
            lbl_prd = sliding_window_inference(img_inp, roi_size, sw_batch_size, self.model)
            lbl_prd = [AsDiscrete(threshold=0.5)(lbl) for lbl in decollate_batch(lbl_prd)]
            lbl_inp = [AsDiscrete(threshold=0.5)(lbl) for lbl in decollate_batch(lbl_inp)]
            # convert tensors from one hot encoding to single channel
            lbl_out = torch.argmax(lbl_prd, dim=1).cpu().numpy()[0, :, :, :]
            # nib.save(os.path.join(self.params["dir_model"], f'{self.params["experiment"]} params.dat')
            # img_array = lbl_inp.cpu().numpy()[0, 0, :, :, :]

            # lbl_array_prd = torch.argmax(prd_val, dim=1).cpu().numpy()[0, :, :, :]
            print(f"Plotting sample {step_val}...")
            # Find the (x,y,z) coordinates of the sample center
            # lbl_array_val = label(lbl_array_val, connectivity=1)
            # lbl_array_prd = label(lbl_array_prd, connectivity=1)
            # Generate overlay images

            # plt.savefig(os.path.join(self.params["dir_model"], f"{self.params['experiment']} img {step_val:02d} xyz {x, y, z} box.png"), dpi=1200)
            # plt.close()