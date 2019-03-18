'''Pytorch models'''

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

import lie_tools
import so3_grid

class ResidLinear(nn.Module):
    def __init__(self, nin, nout):
        super(ResidLinear, self).__init__()
        self.linear = nn.Linear(nin, nout)

    def forward(self, x):
        z = self.linear(x) + x
        return z

class HetVAE(nn.Module):
    def __init__(self, 
            nx, ny, 
            encode_layers, encode_dim, 
            decode_layers, decode_dim,
            z_dim = 1,
            encode_mode = 'mlp',
            ):
        super(HetVAE, self).__init__()
        self.nx = nx
        self.ny = ny
        self.in_dim = nx*ny
        self.z_dim = z_dim
        if encode_mode == 'conv':
            self.encoder = ConvEncoder(encode_dim, z_dim*2)
        elif encode_mode == 'resid':
            self.encoder = ResidLinearEncoder(nx*ny, 
                            encode_layers, 
                            encode_dim,  # hidden_dim
                            z_dim*2, # out_dim
                            nn.ReLU) #in_dim -> hidden_dim
        elif encode_mode == 'mlp':
            self.encoder = MLPEncoder(nx*ny, 
                            encode_layers, 
                            encode_dim, # hidden_dim
                            z_dim*2, # out_dim
                            nn.ReLU) #in_dim -> hidden_dim
        else:
            raise RuntimeError('Encoder mode {} not recognized'.format(encode_mode))
        self.decoder = ResidLinearDecoder(3+z_dim, decode_layers, 
                            decode_dim, 
                            nn.ReLU) #R3 -> R1
        
        # centered and scaled xy plane, values between -1 and 1
        x0, x1 = np.meshgrid(np.linspace(-1, 1, nx, endpoint=False), # FT is not symmetric around origin
                             np.linspace(-1, 1, ny, endpoint=False))
        lattice = np.stack([x0.ravel(),x1.ravel(),np.zeros(ny*nx)],1).astype(np.float32)
        self.lattice = torch.tensor(lattice)
    
    def reparameterize(self, mu, logvar):
        if not self.training:
            return mu
        std = torch.exp(.5*logvar)
        eps = torch.randn_like(std)
        return eps*std + mu

    def encode(self, img):
        z = self.encoder(img)
        return z[:,:self.z_dim], z[:,self.z_dim:]

    def decode(self, coords, z):
        z = z.view(z.size(0), *([1]*(coords.ndimension()-1)))
        z = torch.cat((coords,z.expand(*coords.shape[:-1],1)),dim=-1)

        #z = torch.cat((coords, z[:,None,:].expand(-1,self.in_dim,-1)), dim=-1)
        y_hat = self.decoder(z)
        y_hat = y_hat.view(-1, self.ny, self.nx)
        return y_hat

    def forward(self, rot, z):
        #mu, logvar = self.encode(img)
        #z = self.reparameterize(mu, logvar)
        # transform lattice by rot
        x = self.lattice @ rot # R.T*x
        y_hat = self.decode(x,z)
        return y_hat

class BNBOpt():
    def __init__(self, model, ny, nx):
        super(BNBOpt, self).__init__()
        self.ny = ny
        self.nx = nx
        self.model = model
        x0, x1 = np.meshgrid(np.linspace(-1, 1, nx, endpoint=False), # FT is not symmetric around origin
                             np.linspace(-1, 1, ny, endpoint=False))
        lattice = np.stack([x0.ravel(),x1.ravel(),np.zeros(ny*nx)],1).astype(np.float32)
        self.lattice = torch.tensor(lattice)
        self.base_quat = so3_grid.base_SO3_grid()
        self.base_rot = lie_tools.quaternions_to_SO3(torch.tensor(self.base_quat))
        
    def eval_base_grid(self, images):
        '''Evaluate the base grid for a batch of imges'''
        x = self.lattice @ self.base_rot
        y_hat = self.model(x)
        y_hat = y_hat.view(-1, self.ny, self.nx)
        y_hat = y_hat.unsqueeze(0) # 1xQxYxX
        images = images.unsqueeze(0).transpose(0,1) # Bx1xYxX
        err = torch.sum((images-y_hat).pow(2),(-1,-2)) # BxQ
        mini = torch.argmin(err,1) # B
        return mini.cpu().numpy()
        
    def eval_incremental_grid(self, images, quat_for_image):
        rot = lie_tools.quaternions_to_SO3(torch.tensor(np.concatenate(quat_for_image)))
        x = self.lattice @ rot
        y_hat = self.model(x)
        y_hat = y_hat.view(-1, 8, self.ny, self.nx)
        images = images.unsqueeze(1)
        err = torch.sum((images-y_hat).pow(2),(-1,-2)) # BxQ
        mini = torch.argmin(err,1) # B
        return mini.cpu().numpy()

    def opt_theta(self, images, niter=5):
        B = images.size(0)
        assert not self.model.training
        with torch.no_grad():
            min_i = self.eval_base_grid(images) # 576  model iterations
            min_quat = self.base_quat[min_i]
            s2i, s1i = so3_grid.get_base_indr(min_i)
            for iter_ in range(1,niter+1):
                neighbors = [so3_grid.get_neighbor(min_quat[i], s2i[i], s1i[i], iter_) for i in range(B)]
                quat = [x[0] for x in neighbors]
                ind = [x[1] for x in neighbors]
                min_i = self.eval_incremental_grid(images, quat)
                min_ind = np.stack(ind)[np.arange(B), min_i]
                s2i, s1i = min_ind.T
                min_quat = np.stack(quat)[np.arange(B),min_i]
        return lie_tools.quaternions_to_SO3(torch.tensor(min_quat))

class BNBHetOpt():
    def __init__(self, model, ny, nx):
        super(BNBHetOpt, self).__init__()
        self.ny = ny
        self.nx = nx
        self.model = model # this is the VAE module
        self.base_quat = so3_grid.base_SO3_grid()
        self.nbase = len(self.base_quat)
        self.base_rot = lie_tools.quaternions_to_SO3(torch.tensor(self.base_quat))
        
    def eval_base_grid(self, images, z):
        '''Evaluate the base grid for a batch of imges'''
        B = z.size(0) 
        x = self.model.lattice @ self.base_rot # Q x (Y*X) x 3
        images = images.unsqueeze(0).transpose(0,1) # Bx1xYxX

        # the mini-mini-batch size, since we need to evaluate the whole 576 point grid for each image
        nB = int(9000/self.ny/self.nx) # huge hack to avoid out of memory error
        assert B % nB == 0, 'Batch size needs to be a multiple of {}'.format(nB) # TODO
        mini = []
        for i in range(int(B/nB)):
            xx = x.expand(nB, self.nbase, self.ny*self.nx, 3) # B x Q x (Y*X) x 3
            y_hat = self.model.decode(xx, z[nB*i:nB*(i+1)]) 
            y_hat = y_hat.view(nB, self.nbase, self.ny, self.nx) # B x Q x Y x X
            img = images[nB*i:nB*(i+1)]
            err = torch.sum((img-y_hat).pow(2),(-1,-2)) # BxQ
            mini.append(torch.argmin(err,1)) # B
        return torch.cat(mini).cpu().numpy()
        
    def eval_incremental_grid(self, images, quat_for_image, z):
        rot = lie_tools.quaternions_to_SO3(torch.tensor(np.array(quat_for_image)))
        x = self.model.lattice @ rot
        y_hat = self.model.decode(x, z) 
        y_hat = y_hat.view(-1, 8, self.ny, self.nx)
        images = images.unsqueeze(1)
        err = torch.sum((images-y_hat).pow(2),(-1,-2)) # BxQ
        mini = torch.argmin(err,1) # B
        return mini.cpu().numpy()

    def opt_theta(self, images, z, niter=5):
        B = images.size(0)
        assert not self.model.training
        with torch.no_grad():
            min_i = self.eval_base_grid(images, z) # 576  model iterations
            min_quat = self.base_quat[min_i]
            s2i, s1i = so3_grid.get_base_indr(min_i)
            for iter_ in range(1,niter+1):
                neighbors = [so3_grid.get_neighbor(min_quat[i], s2i[i], s1i[i], iter_) for i in range(B)]
                quat = [x[0] for x in neighbors]
                ind = [x[1] for x in neighbors]
                min_i = self.eval_incremental_grid(images, quat, z)
                min_ind = np.stack(ind)[np.arange(B), min_i]
                s2i, s1i = min_ind.T
                min_quat = np.stack(quat)[np.arange(B),min_i]
        return lie_tools.quaternions_to_SO3(torch.tensor(min_quat))

class ResidLinearDecoder(nn.Module):
    '''
    A NN mapping R3 cartesian coordinates to R1 electron density
    (represented in Hartley reciprocal space)
    '''
    def __init__(self, in_dim, nlayers, hidden_dim, activation):
        super(ResidLinearDecoder, self).__init__()
        layers = [nn.Linear(in_dim, hidden_dim), activation()]
        for n in range(nlayers):
            layers.append(ResidLinear(hidden_dim, hidden_dim))
            layers.append(activation())
        layers.append(nn.Linear(hidden_dim,1))
        self.main = nn.Sequential(*layers)

    def forward(self, x):
        return self.main(x)

class ResidLinearEncoder(nn.Module):
    def __init__(self, in_dim, nlayers, hidden_dim, out_dim, activation):
        super(ResidLinearEncoder, self).__init__()
        self.in_dim = in_dim
        # define network
        layers = [nn.Linear(in_dim, hidden_dim), activation()]
        for n in range(nlayers-1):
            layers.append(ResidLinear(hidden_dim, hidden_dim))
            layers.append(activation())
        layers.append(nn.Linear(hidden_dim, out_dim))
        self.main = nn.Sequential(*layers)

    def forward(self, img):
        return self.main(img.view(-1,self.in_dim))

class MLPEncoder(nn.Module):
    def __init__(self, in_dim, nlayers, hidden_dim, out_dim, activation):
        super(MLPEncoder, self).__init__()
        self.in_dim = in_dim
        # define network
        layers = [nn.Linear(in_dim, hidden_dim), activation()]
        for n in range(nlayers-1):
            layers.append(nn.Linear(hidden_dim, hidden_dim))
            layers.append(activation())
        layers.append(nn.Linear(hidden_dim, out_dim))
        self.main = nn.Sequential(*layers)

    def forward(self, img):
        return self.main(img.view(-1,self.in_dim))
      
# Adapted from soumith DCGAN
class ConvEncoder(nn.Module):
    def __init__(self, hidden_dim, out_dim):
        super(ConvEncoder, self).__init__()
        ndf = hidden_dim
        self.main = nn.Sequential(
            # input is 1 x 64 x 64
            nn.Conv2d(1, ndf, 4, 2, 1, bias=False),
            nn.LeakyReLU(0.2, inplace=True),
            # state size. (ndf) x 32 x 32
            nn.Conv2d(ndf, ndf * 2, 4, 2, 1, bias=False),
            nn.BatchNorm2d(ndf * 2),
            nn.LeakyReLU(0.2, inplace=True),
            # state size. (ndf*2) x 16 x 16
            nn.Conv2d(ndf * 2, ndf * 4, 4, 2, 1, bias=False),
            nn.BatchNorm2d(ndf * 4),
            nn.LeakyReLU(0.2, inplace=True),
            # state size. (ndf*4) x 8 x 8
            nn.Conv2d(ndf * 4, ndf * 8, 4, 2, 1, bias=False),
            nn.BatchNorm2d(ndf * 8),
            nn.LeakyReLU(0.2, inplace=True),
            # state size. (ndf*8) x 4 x 4
            nn.Conv2d(ndf * 8, out_dim, 4, 1, 0, bias=False),
            # state size. out_dims x 1 x 1
        )
    def forward(self, x):
        x = torch.unsqueeze(x,1)
        x = self.main(x)
        return x.view(x.size(0), -1) # flatten

### not used in this branch ###

class SO3reparameterize(nn.Module):
    '''Reparameterize R^N encoder output to SO(3) latent variable'''
    def __init__(self, input_dims):
        super().__init__()
        self.s2s2map = nn.Linear(input_dims, 6)
        self.so3var = nn.Linear(input_dims, 3)

        # start with big outputs
        #self.s2s2map.weight.data.uniform_(-5,5)
        #self.s2s2map.bias.data.uniform_(-5,5)

    def sampleSO3(self, z_mu, z_std):
        '''
        Reparameterize SO(3) latent variable
        # z represents mean on S2xS2 and variance on so3, which enocdes a Gaussian distribution on SO3
        # See section 2.5 of http://ethaneade.com/lie.pdf
        '''
        # resampling trick
        eps = torch.randn_like(z_std)
        w_eps = eps*z_std
        rot_eps = lie_tools.expmap(w_eps)
        rot_sampled = z_mu @ rot_eps
        return rot_sampled, w_eps

    def forward(self, x):
        z = self.s2s2map(x).double()
        logvar = self.so3var(x)
        z_mu = lie_tools.s2s2_to_SO3(z[:, :3], z[:, 3:]).float()
        z_std = torch.exp(.5*logvar) # or could do softplus
        return z_mu, z_std 

        

