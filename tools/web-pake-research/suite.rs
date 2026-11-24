// SPDX-License-Identifier: GPL-3.0-or-later
use opaque_ke::{argon2::Argon2, CipherSuite, Ristretto255, TripleDh};

pub struct Suite;

impl CipherSuite for Suite {
    type OprfCs = Ristretto255;
    type KeyExchange = TripleDh<Ristretto255, <Ristretto255 as voprf::CipherSuite>::Hash>;
    type Ksf = Argon2<'static>;
}
