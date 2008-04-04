"""Methods to smooth tentative prolongation operators"""

__docformat__ = "restructuredtext en"

from pyamg.utils import approximate_spectral_radius, scale_rows

__all__ = ['jacobi_prolongation_smoother', 'energy_prolongation_smoother']


def jacobi_prolongation_smoother(S, T, omega=4.0/3.0):
    """Jacobi prolongation smoother
   
    Parameters
    ----------
    S : {csr_matrix, bsr_matrix}
        Sparse NxN matrix used for smoothing.  Typically, A or the
        "filtered matrix" obtained from A by lumping weak connections
        onto the diagonal of A.
    T : {csr_matrix, bsr_matrix}
        Tentative prolongator
    omega : {scalar}
        Damping parameter

    Returns
    -------
    P : {csr_matrix, bsr_matrix}
        Smoothed (final) prolongator defined by P = (I - omega/rho(S) S) * T
        where rho(S) is an approximation to the spectral radius of S.

    """

    D = S.diagonal()
    D_inv = 1.0 / D
    D_inv[D == 0] = 0

    D_inv_S = scale_rows(S, D_inv, copy=True)
    D_inv_S *= omega/approximate_spectral_radius(D_inv_S)

    P = T - (D_inv_S*T)

    return P





""" sa_energy_min + helper functions minimize the energy of a tentative prolongator for use in SA """

from numpy import array, zeros, matrix, mat
from scipy.sparse import csr_matrix, isspmatrix_csr, bsr_matrix, isspmatrix_bsr, spdiags
from scipy.linalg import svd, norm
import pyamg
from pyamg.utils import UnAmal, BSR_Get_Colindices, BSR_Get_Row
from scipy.io import loadmat, savemat

########################################################################################################
#   Helper function for the energy minimization prolongator generation routine

def Satisfy_Constraints_CSR(U, Sparsity_Pattern, B, BtBinv):
    """Update U to satisfy U*B = 0

    Input
    =====
    U                       CSR Matrix to operate on
    Sparsity_Pattern        Sparsity pattern to enforce
    B                       Near nullspace vectors
    BtBinv                  Local inv(B'*B) matrices for each dof, i.  
        
    Output
    ======
    Updated U, so that U*B = 0.  Update is computed by orthogonal (in 2-norm)
    projecting out the components of span(B) in U in a row-wise fashion

    """
    
    Nfine = U.shape[0]
    
    UB = U*mat(B)
    #Project out U's components in span(B) row-wise
    for i in range(Nfine):
        rowstart = Sparsity_Pattern.indptr[i]
        rowend = Sparsity_Pattern.indptr[i+1]
        length = rowend - rowstart
        colindx = Sparsity_Pattern.indices[rowstart:rowend]
        
        if(length != 0):
            Bi = B[colindx,:]
            UBi = UB[i,:]
    
            update_local = (Bi*(BtBinv[i]*UBi.T)).T
            Sparsity_Pattern.data[rowstart:rowend] = update_local
    
    #Now add in changes from Sparsity_Pattern to U.  We don't write U directly in the above loop,
    #   because its possible, once in a blue moon, to have the sparsity pattern of U be a subset
    #   of the pattern for Sparsity_Pattern.  This is more expensive, but more robust.
    U = U - Sparsity_Pattern
    Sparsity_Pattern.data[:] = 1.0
    return U


def Satisfy_Constraints_BSR(U, Sparsity_Pattern, B, BtBinv, colindices):
    """Update U to satisfy U*B = 0

    Input
    =====
    U                     BSR Matrix to operate on
    Sparsity_Pattern      Sparsity pattern to enforce
    B                     Near nullspace vectors
    BtBinv                Local inv(B'*B) matrices for each dof, i.  
    colindices            List indexed by node that returns column indices for
                          all dof's in that node.  Assumes that each block is 
                          perfectly dense.  The below code does assure this 
                          perfect denseness.
        
    Output
    ======
    Updated U, so that U*B = 0.  Update is computed by orthogonal (in 2-norm)
    projecting out the components of span(B) in U in a row-wise fashion

    """

    Nfine = U.shape[0]
    RowsPerBlock = U.blocksize[0]
    ColsPerBlock = U.blocksize[1]
    Nnodes = Nfine/RowsPerBlock
    
    UB = U*mat(B)
    
    rowoffset = 0
    for i in range(Nnodes):
        rowstart = Sparsity_Pattern.indptr[i]
        rowend = Sparsity_Pattern.indptr[i+1]
        colindx = colindices[i]
        length = len(colindx)
        numBlocks = rowend-rowstart
        
        if(length != 0):
            Bi = B[colindx,:]
            UBi = UB[rowoffset:(rowoffset+RowsPerBlock), :]
            update_local = (Bi*(BtBinv[i]*UBi.T))
    
            #Write node's values 
            for j in range(RowsPerBlock):
                Sparsity_Pattern.data[rowstart:rowend, j, :] = update_local[:,j].reshape(numBlocks, ColsPerBlock)
    
        rowoffset += RowsPerBlock
    
    #Now add in changes from Sparsity_Pattern to U.  We don't write U directly in the above loop,
    #   because its possible, once in a blue moon, to have the sparsity pattern of U be a subset
    #   of the pattern for Sparsity_Pattern.  This is more expensive, but more robust.
    U = U - Sparsity_Pattern
    Sparsity_Pattern.data[:,:,:] = 1.0
    return U


########################################################################################################


def energy_prolongation_smoother(A, T, Atilde, B, SPD=True, num_iters=4, min_tol=1e-8, file_output=False):
    """Minimize the energy of the coarse basis functions (columns of T)

    Parameters
    ----------
    A : {csr_matrix, bsr_matrix}
        Sparse NxN matrix
    T : {bsr_matrix}
        Tentative prolongator, a NxM sparse matrix (M < N)
    Atilde : {csr_matrix}
        Strength of connection matrix
    B : {array}
        Near-nullspace modes for coarse grid.  Has shape (M,k) where
        k is the number of coarse candidate vectors.
    SPD : boolean
        Booolean denoting symmetric positive-definiteness of A
    num_iters : integer
        Number of energy minimization steps to apply to the prolongator
    min_tol : scalar
        Minimization tolerance
    file_output : boolean
        Optional diagnostic file output of matrices


    Returns
    -------
    P : {bsr_matrix}
        Smoothed prolongator

    References
    ----------

        Mandel
        "Energy Optimization of Algebraic Multigrid Bases."

        TODO COMPLETE REF
    
    """
    
    #====================================================================
    #Test Inputs
    if( not(isinstance(num_iters,int)) ):
        raise TypeError("\nCalling sa_energy_min Incorrectly.  "
                "Number of minimization iterations applied to P is \"num_iters\", where \"num_iters\" must be an integer.\n\n")
    if( num_iters < 0 ):
        raise TypeError("\nCalling sa_energy_min Incorrectly.  ",  
                "Number of minimization iterations applied to P is \"num_iters\", where \"num_iters\" >= 0.\n\n")
    if( min_tol > 1  ):
        raise TypeError("\nCalling sa_energy_min Incorrectly.  0 < \"min_tol\" < 1.\n\n")
    
    csrflag = isspmatrix_csr(A)
    if( not(csrflag) and (isspmatrix_bsr(A) == False)):
        raise TypeError("sa_energy_min routine requires a CSR or BSR operator.  Aborting.\n")
    if(isspmatrix_bsr(T) == False):
        raise TypeError("sa_energy_min routine requires a BSR tentative prolongator.  Aborting.\n")
    if( not(csrflag) and (T.blocksize[0] != A.blocksize[0]) ):
        print "Warning, T's row-blocksize should be the same as A's blocksize.\n"
    if( (T.nnz == 0) or (Atilde.nnz == 0) or (A.nnz == 0) ):
        print "Error in sa_energy_min(..).  A, T or Atilde has no nonzeros on a level."
        return T

    #====================================================================
    
    
    #====================================================================
    # Retrieve problem information
    Nfine = T.shape[0]
    Ncoarse = T.shape[1]
    NullDim = B.shape[1]
    if(csrflag):
        numPDEs = 1
    else:
        #Number of PDEs per point is defined implicitly by block size
        numPDEs = A.blocksize[0]
    #====================================================================
    
    
    #====================================================================
    # Construct Dinv and Unamalgate Atilde if (numPDEs > 1)
    D = A.diagonal();
    #Must Do extensive checking for 0 rows of A.
    if(D.nonzero()[0].shape[0] != A.shape[0]):
        zero_rows = D.__eq__(0.0).nonzero()[0]
        for i in zero_rows:
            #See if row i is all zero
            if(csrflag):
                if(A.indptr[i] != A.indptr[i+1]):
                    raise ValueError("Zero on diag(A) for nonzero row of A in sa_ode_energy_min routine. -- Aborting.\n\n")
            else:
                zi, colindx = BSR_Get_Row(A, i)
                if(min(colindx.shape) > 0):
                    raise ValueError("Zero on diag(A) for nonzero row of A in sa_ode_energy_min routine. -- Aborting.\n\n") 
        # Zeros on D represent 0 rows, so we can just set D to 1.0 at those locations and then Dinv*A 
        #   at the zero rows of A will still be zero
        D[zero_rows] = 1.0

    if(csrflag):
        Dinv    = spdiags( [1.0/D], [0], Nfine, Nfine, format = 'csr')
    else:
        Dinv    = spdiags( [1.0/D], [0], Nfine, Nfine, format = 'bsr')
        Dinv = bsr_matrix(Dinv, blocksize=A.blocksize)
    
    #If A is BSR, then Atilde needs to be "UnAmalgamated" to generate prolongator
    #   sparsity pattern.  Sparsity pattern is generated through matrix multiplication
    if(not(csrflag)):
        #UnAmal returns a BSR matrix, so the mat-mat will be a BSR mat-mat.  Unfortunately, 
        #   we also need column indices for Sparsity_Pattern
        Sparsity_Pattern = UnAmal(Atilde, numPDEs, numPDEs).__abs__()*T.__abs__()
        Sparsity_Pattern.data[:,:,:] = 1.0
        Sparsity_Pattern.sort_indices()
        colindices = BSR_Get_Colindices(Sparsity_Pattern)
    else:
        #Sparsity_Pattern will be CSR as Atilde is CSR.  This means T will be converted to
        #   CSR, but we need Sparsity_Pattern in CSR.  T is converted back to BSR at the end.
        blocks = T.blocksize
        T = T.tocsr()
        Sparsity_Pattern = Atilde.__abs__()*T.__abs__()
        Sparsity_Pattern.data[:] = 1.0
        Sparsity_Pattern.sort_indices()
    #====================================================================
    
    #====================================================================
    #Optional file output for diagnostic purposes
    if(file_output == True):
        savemat('Sparsity_Pattern', { 'Sparsity_Pattern' : Sparsity_Pattern.toarray() } ) 
        savemat('Amat', { 'Amat' : A.toarray() } ) 
        savemat('Atilde', { 'Atilde' : Atilde.toarray() } )
        savemat('P', { 'P' : T.toarray() } ) 
        savemat('ParamsEnMin', {'nPDE' : numPDEs, 'Nits' : num_iters, 'SPD' : SPD } ) 
        savemat('Bone', { 'Bone' : array(B) } )
    #====================================================================
    
    
    #====================================================================
    #Construct array of inv(Bi'Bi), where Bi is B restricted to row i's sparsity pattern in 
    #   Sparsity Pattern.  This array is used multiple times in the Satisfy_Constraints routine.
    if(csrflag):
        preall = zeros((NullDim,NullDim))
        BtBinv = [matrix(preall,copy=True) for i in range(Nfine)]
        del preall
        B = mat(B)
        for i in range(Nfine):
            rowstart = Sparsity_Pattern.indptr[i]
            rowend = Sparsity_Pattern.indptr[i+1]
            length = rowend - rowstart
            colindx = Sparsity_Pattern.indices[rowstart:rowend]
    
            if(length != 0):
                Bi = B[colindx,:]
                #Calculate SVD as system may be singular
                U,Sigma,VT = svd(Bi.T*Bi)
                
                #Filter Sigma and calculate inv(Sigma)
                if(abs(Sigma[0]) < 1e-10):
                    Sigma[:] = 0.0
                else:
                    #Zero out "numerically" zero singular values
                    #   Efficiency TODO -- would this be faster in a loop that starts from the
                    #   back of Sigma and assumes Sigma is sorted?  Experiments say no.
                    Sigma =  (Sigma/Sigma[0]).__abs__().__gt__(1e-8)*Sigma
                        
                    #Test for any zeros in Sigma
                    if(Sigma[NullDim-1] == 0.0):
                        #Truncate U, VT and Sigma w.r.t. zero entries in Sigma
                        indys = Sigma.nonzero()[0]
                        Sigma = Sigma[indys]
                        U = U[:,indys]
                        VT = VT[indys,:]
                            
                    #Invert nonzero sing values
                    Sigma = 1.0/Sigma
                
                #Calculate inverse
                BtBinv[i] = mat(VT.T)*mat(Sigma.reshape(Sigma.shape[0],1)*U.T)
    else:   #BSR matrix
        preall = zeros((NullDim,NullDim))
        RowsPerBlock = Sparsity_Pattern.blocksize[0]
        Nnodes = Nfine/RowsPerBlock
        BtBinv = [matrix(preall,copy=True) for i in range(Nnodes)]
        del preall
        B = mat(B)
        for i in range(Nnodes):
                
            rowstart = Sparsity_Pattern.indptr[i]
            rowend = Sparsity_Pattern.indptr[i+1]
            length = rowend - rowstart
            colindx = colindices[i]
    
            if(length != 0):
                Bi = B[colindx,:]
                #Calculate SVD as system may be singular
                U,Sigma,VT = svd(Bi.T*Bi)
                
                #Filter Sigma and calculate inv(Sigma)
                if(abs(Sigma[0]) < 1e-10):
                    Sigma[:] = 0.0
                else:
                    #Zero out "numerically" zero singular values
                    #   Efficiency TODO -- would this be faster in a loop that starts from the
                    #   back of Sigma and assumes Sigma is sorted?  Experiments say no.
                    Sigma =  (Sigma/Sigma[0]).__abs__().__gt__(1e-8)*Sigma
                        
                    #Test for any zeros in Sigma
                    if(Sigma[NullDim-1] == 0.0):
                        #Truncate U, VT and Sigma w.r.t. zero entries in Sigma
                        indys = Sigma.nonzero()[0]
                        Sigma = Sigma[indys]
                        U = U[:,indys]
                        VT = VT[indys,:]
                            
                    #Invert nonzero sing values
                    Sigma = 1.0/Sigma
                
                #Calculate inverse
                BtBinv[i] = mat(VT.T)*mat(Sigma.reshape(Sigma.shape[0],1)*U.T)
    
    #====================================================================
    
    
    #====================================================================
    #Prepare for Energy Minimization
    #Calculate initial residual
    R = -A*T
    
    #Enforce constraints on R.  First the sparsity pattern, then the nullspace vectors.
    R = R.multiply(Sparsity_Pattern)
    if(csrflag):
        R = Satisfy_Constraints_CSR(R, Sparsity_Pattern, B, BtBinv)
    else:
        R = Satisfy_Constraints_BSR(R, Sparsity_Pattern, B, BtBinv, colindices)
    if(R.nnz == 0 ):
        print "Error in sa_energy_min(..).  Initial R no nonzeros on a level.  Calling Default Prolongator Smoother\n"
        T = pyamg.sa.sa_smoothed_prolongator(A,T)
        return T
    
    #Calculate max norm of the residual
    resid = max(R.data.flatten().__abs__())
    #print "Energy Minimization of Prolongator --- Iteration 0 --- r = " + str(resid)
    #====================================================================
    
    
    #====================================================================
    #Iteratively minimize the energy of T subject to the constraints of Sparsity_Pattern
    #   and maintaining T's effect on B, i.e. T*B = (T+Update)*B, i.e. Update*B = 0 
    i = 0
    if(SPD):
        #Apply CG
        while( (i < num_iters) and (resid > min_tol) ):
            #Apply diagonal preconditioner
            Z = Dinv*R
    
            #Frobenius innerproduct of (R,Z) = sum(rk.*zk)
            newsum = (R.multiply(Z)).sum()
                
            #P is the search direction, not the prolongator, which is T.    
            if(i == 0):
                P = Z
            else:
                beta = newsum/oldsum
                P = Z + beta*P
            oldsum = newsum
    
            #Calculate new direction and enforce constraints
            AP = A*P
            AP = AP.multiply(Sparsity_Pattern)
            if(csrflag):
                AP = Satisfy_Constraints_CSR(AP, Sparsity_Pattern, B, BtBinv)
            else:
                AP = Satisfy_Constraints_BSR(AP, Sparsity_Pattern, B, BtBinv, colindices)
            
            #Frobenius innerproduct of (P, AP)
            alpha = newsum/(P.multiply(AP)).sum()
    
            #Update the prolongator, T
            T = T + alpha*P 
    
            #Update residual
            R = R - alpha*AP
            
            i += 1
            resid = max(R.data.flatten().__abs__())
            #print "Energy Minimization of Prolongator --- Iteration " + str(i) + " --- r = " + str(resid)
            
    else:
        #Apply min-res to the nonsymmetric system
        while( (i < num_iters) and (resid > min_tol) ):
    
            #P is the search direction, not the prolongator
            P = A*R
    
            #Enforce constraints on P
            P = P.multiply(Sparsity_Pattern)
            if(csrflag):
                P = Satisfy_Constraints_CSR(P, Sparsity_Pattern, B, BtBinv)
            else:
                P = Satisfy_Constraints_BSR(P, Sparsity_Pattern, B, BtBinv, colindices)
    
            #Frobenius innerproduct of (P, R)
            numer = (P.multiply(R)).sum()
            
            #Frobenius innerproduct of (P, P)
            denom = (P.multiply(P)).sum()
    
            alpha = numer/denom
    
            #Update prolongator
            T = T + alpha*R
    
            #Update residual
            R = R - alpha*P
            
            i += 1
            resid = max(R.data.flatten().__abs__())
            #print "Energy Minimization of Prolongator --- Iteration " + str(i) + " --- r = " + str(resid)
    #====================================================================
    
    
    #====================================================================
    #Make sure T is in correct block format.
    if(csrflag):
        T = T.tobsr(blocksize=blocks)
    #====================================================================
    
    
    #====================================================================
    #Optional file output for diagnostic purposes
    if(file_output == True):
        savemat('Ppyth', { 'Ppyth' : T.toarray() } ) 
    #====================================================================
    
    
    return T

