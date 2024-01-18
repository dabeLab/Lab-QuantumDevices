import numpy as np
import SimpleITK as sitk
from skimage.segmentation import felzenszwalb
import matplotlib.pyplot as plt


def cosines_to_patient(direction_cosines):
    # Convert the direction cosines to a 3x2 matrix
    matrix = np.array(direction_cosines).reshape((3, 2))
    # Determine orientation labels
    orientation_labels = []

    # determines the sign of the angle between the image first row and the right-to-left patient direction
    if matrix[0, 0] > 0:
        orientation_labels.append('R')
    elif matrix[0, 0] < 0:
        orientation_labels.append('L')

    # determines the sign of the angle between the image first column and the anterior-to-posterior patient direction
    if matrix[1, 1] > 0:
        orientation_labels.append('A')
    elif matrix[1, 1] < 0:
        orientation_labels.append('P')

    # determines the sign of the angle between the image first row and the head(S)-to-feet(I) patient direction
    if matrix[2, 0] > 0:
        orientation_labels.append('S')
    elif matrix[2, 0] < 0:
        orientation_labels.append('I')

    # Join orientation labels to get the final orientation
    orientation = ''.join(orientation_labels)

    return orientation


def read_dicom_series(path_dicom_series):
    """Read a DICOM series and convert it to 3D nifti image"""
    # Load the DICOM series
    reader = sitk.ImageSeriesReader()
    dicom_series = reader.GetGDCMSeriesFileNames(path_dicom_series)
    reader.SetFileNames(dicom_series)
    image = reader.Execute()
    # Convert the SimpleITK image to NIfTI format in memory
    # nifti_image = sitk.GetImageFromArray(sitk.GetArrayFromImage(image))
    # nifti_image.CopyInformation(image)
    # Convert the SimpleITK image to NIfTI format
    # sitk.WriteImage(image, path_nifti)
    return image


def register_brain(fix_img: sitk.Image, mov_img: sitk.Image, registration="rigid"):
    """ This function registers a brain atlas (moving image) to an MR image (fixed image).
    To facilitate the registration, the function consists in the following steps:
    1. Resample the moving image to the fixed image by 3D affine transformation. This allows to match the spacings,
    the origins and to align the direction cosines of the moving image to the fixed image.
    2. Match the intensity histogram of the moving image to the intensity histogram of the fixed image. This is
    necessary to make the image intensities consistent.
    3. Initialize the registration by preliminary placing the moving image in the vicinity of the .
    To this purpose, the MR image is segmented with the Felzenszwalb algorithm, and the region corresponding to
    the brain selected as the one whose centroid is closest to a reference point, assigned to each anatomical plane
    (sagittal, dorsal and transverse), and representing the average brain’s position. This reference point is
    selected by the user by left-click on the sagittal, dorsal and transverse section of the MR image.
    4. Registration - Rigid. To reduce computational time and help the registration procedure to focus on the brain region, we
    applied a mask to the fixed target image. The mask is chosen to correspond to the atlas brain mask,
    dilated with a 3D ball structuring element of radius 10 pixels.
    5 Registration - Elastic."""

    # Plot

    # 1. Resampling
    # Cast the pixel data type of the moving image to the pixel data type of the fixed image
    mov_img = sitk.Cast(mov_img, fix_img.GetPixelID())
    # Create a 3D affine transformation
    transform = sitk.AffineTransform(3)
    # Set the translation, i.e. the difference between origins
    transform.SetTranslation(np.array(fix_img.GetOrigin()) - np.array(mov_img.GetOrigin()))
    # Set the center of rotation to the center of the fixed image
    # transform.SetCenter(fix_img.TransformContinuousIndexToPhysicalPoint([index / 2.0 for index in fix_img.GetSize()]))
    # Set the rotation matrix
    fix_img_direction_cosines = np.array(fix_img.GetDirection()).reshape((3, 3))
    mov_img_direction_cosines = np.array(mov_img.GetDirection()).reshape((3, 3))
    rotation_matrix = np.dot(np.linalg.inv(fix_img_direction_cosines), mov_img_direction_cosines)
    transform.SetMatrix(rotation_matrix.flatten())

    # resample the moving image (the brain atlas) to fit the fixed image (MR image) space
    mov_img = sitk.Resample(image1=mov_img,  # image to resample
                            referenceImage=fix_img,  # reference image
                            transform=transform,
                            interpolator=sitk.sitkLinear,)  # type of interpolation
    sitk.WriteImage(mov_img, "E:/2021_local_data/2023_Gd_synthesis/atlas/canine transformed.nii.gz")

    # Plot
    check_registration(fix_img, mov_img)
    plt.show()

    # 2. MATCH INTENSITY HISTOGRAMS
    mov_img = sitk.HistogramMatching(image=mov_img, referenceImage=fix_img)
    plt.show()

    # 3. INITIALIZE REGISTRATION - PRE-POSITIONING
    # segment the MR image by graph-method - Felzenswalb. This method works on 2D images.
    felzenszwalb(sitk.GetArrayFromImage(mov_img), scale=3.0, sigma=0.5, min_size=5)

    # 6. REGISTRATION
    parameterMap = sitk.GetDefaultParameterMap(registration)
    #sitk.PrintParameterMap(parameterMap)

    # create an elastic object
    elastixImageFilter = sitk.ElastixImageFilter()
    # set fixed and moving images, and mapping parameters
    elastixImageFilter.SetFixedImage(fix_img)
    elastixImageFilter.SetMovingImage(mov_img)
    elastixImageFilter.SetParameterMap(parameterMap)
    # execute registration
    elastixImageFilter.Execute()
    # get resulting image
    resultImage = elastixImageFilter.GetResultImage()
    # get transformation parameters
    transformParameterMap = elastixImageFilter.GetTransformParameterMap()

    return resultImage


def extract_sagittal_section(img: sitk.Image):
    """It assumes a 3D image"""
    size = img.GetSize()
    spacing = img.GetSpacing()
    n = int(size[0]/2)
    img_slice = sitk.Extract(img, [0, size[1], size[2]], [n, 0, 0])
    plt.figure()
    plt.imshow(sitk.GetArrayFromImage(img_slice), cmap='gray', aspect=spacing[2] / spacing[1])
    plt.axis("off")
    plt.title("sagittal")


def extract_coronal_section(img: sitk.Image):
    """It assumes a 3D image"""
    size = img.GetSize()
    spacing = img.GetSpacing()
    n = int(size[1]/2)
    img_slice = sitk.Extract(img, [size[0], 0, size[2]], [0, n, 0])
    plt.figure()
    plt.imshow(sitk.GetArrayFromImage(img_slice), cmap='gray', aspect=spacing[2] / spacing[0])
    plt.axis("off")
    plt.title("coronal")


def extract_axial_section(img: sitk.Image):
    """It assumes a 3D image"""
    size = img.GetSize()
    spacing = img.GetSpacing()
    n = int(size[2] / 2)
    img_slice = sitk.Extract(img, [size[0], size[1], 0], [0, 0, n])
    plt.figure()
    plt.imshow(sitk.GetArrayFromImage(img_slice), cmap='gray', aspect=spacing[1] / spacing[0])
    plt.axis("off")
    plt.title("axial")


def check_registration(fix_img: sitk.Image, mov_img: sitk.Image, n_slices=2):
    """It assumes a 3D image"""
    fix_img_size = fix_img.GetSize()
    mov_img_size = mov_img.GetSize()
    fix_img_spacing = fix_img.GetSpacing()
    mov_img_spacing = mov_img.GetSpacing()
    fig, ax = plt.subplots(n_slices, 3)
    for idx in range(n_slices):
        nx = int(fix_img_size[0] * (idx + 1) / (n_slices + 1))
        ny = int(fix_img_size[1] * (idx + 1) / (n_slices + 1))
        nz = int(fix_img_size[2] * (idx + 1) / (n_slices + 1))
        fix_img_slice = sitk.Extract(fix_img, [fix_img_size[0], fix_img_size[1], 0], [0, 0, nz])
        mov_img_slice = sitk.Extract(mov_img, [mov_img_size[0], mov_img_size[1], 0], [0, 0, nz])
        ax[idx, 0].imshow(sitk.GetArrayFromImage(fix_img_slice), cmap='gray', aspect=fix_img_spacing[1] / fix_img_spacing[0])
        ax[idx, 0].imshow(sitk.GetArrayFromImage(mov_img_slice), cmap='jet', alpha=0.5, aspect=mov_img_spacing[1] / mov_img_spacing[0])
        ax[idx, 0].set_axis_off()
        ax[idx, 0].set_title("xy - axial")
        fix_img_slice = sitk.Extract(fix_img, [0, fix_img_size[1], fix_img_size[2]], [nx, 0, 0])
        mov_img_slice = sitk.Extract(mov_img, [0, mov_img_size[1], mov_img_size[2]], [nx, 0, 0])
        ax[idx, 1].imshow(sitk.GetArrayFromImage(fix_img_slice), cmap='gray', aspect=fix_img_spacing[2] / fix_img_spacing[1])
        ax[idx, 1].imshow(sitk.GetArrayFromImage(mov_img_slice), cmap='jet', alpha=0.5, aspect=mov_img_spacing[2] / mov_img_spacing[1])
        ax[idx, 1].set_axis_off()
        ax[idx, 1].set_title("yz - sagittal")
        fix_img_slice = sitk.Extract(fix_img, [fix_img_size[0], 0, fix_img_size[2]], [0, ny, 0])
        mov_img_slice = sitk.Extract(mov_img, [mov_img_size[0], 0, mov_img_size[2]], [0, ny, 0])
        ax[idx, 2].imshow(sitk.GetArrayFromImage(fix_img_slice), cmap='gray', aspect=fix_img_spacing[2] / fix_img_spacing[0])
        ax[idx, 2].imshow(sitk.GetArrayFromImage(mov_img_slice), cmap='jet', alpha=0.5, aspect=mov_img_spacing[2] / mov_img_spacing[0])
        ax[idx, 2].set_axis_off()
        ax[idx, 2].set_title("xz - coronal")
