from ipykernel.kernelapp import IPKernelApp
from .kernel import SPARKDiscKernel
IPKernelApp.launch_instance(kernel_class=SPARKDiscKernel)
