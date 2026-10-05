// 1-D valid convolution: y[f][o] = sum_k x[o+k] * w[f][k]
// x: IN_LEN int8, w: F filters of K int8, y: F*(IN_LEN-K+1) int32, row-major.
// Independent valid/ready streams, backpressure, back-to-back transactions.

module dut #(
    parameter IN_LEN = 128,
    parameter F = 4,
    parameter K = 4,
    parameter LANES = 16,
    parameter DATA_W = 8,
    parameter ACC_W = 32
)(
    input  wire                    clk,
    input  wire                    rst_n,
    input  wire [LANES*DATA_W-1:0] in_x_flat,
    input  wire                    in_x_flat_valid,
    output wire                    in_x_flat_ready,
    input  wire [LANES*DATA_W-1:0] in_w_flat,
    input  wire                    in_w_flat_valid,
    output wire                    in_w_flat_ready,
    output wire                    out_valid,
    input  wire                    out_ready,
    output wire signed [ACC_W-1:0] out_c
);

    localparam integer OUT_POS  = IN_LEN - K + 1;          // 125
    localparam integer X_BEATS  = IN_LEN / LANES;          // 8
    localparam integer W_ELEMS  = F * K;                   // 16
    localparam integer REALIGN  = K - 1;                   // extra rotates to restore x
    localparam integer POS_W    = 7;                       // covers 0..124
    localparam integer FILT_W   = 2;

    localparam S_LOAD = 1'b0;
    localparam S_RUN  = 1'b1;

    reg                  state;
    reg [3:0]            x_beat;
    reg                  w_loaded;
    reg [FILT_W-1:0]     filt;
    reg [POS_W-1:0]      pos;
    reg [1:0]            realign_left;

    // x lives in a circular shift register. Window is always x_sr[0 +: K].
    // One rotate toward index 0 advances the convolution position by 1.
    // After OUT_POS rotates, K-1 more rotates restore the original alignment
    // so the next filter can rescan the same samples.
    reg signed [DATA_W-1:0] x_sr  [0:IN_LEN-1];
    reg signed [DATA_W-1:0] w_mem [0:W_ELEMS-1];

    assign in_x_flat_ready = (state == S_LOAD) && (x_beat < X_BEATS[3:0]);
    assign in_w_flat_ready = (state == S_LOAD) && !w_loaded;
    assign out_valid       = (state == S_RUN) && (realign_left == 2'd0);

    wire x_accept = in_x_flat_valid && in_x_flat_ready;
    wire w_accept = in_w_flat_valid && in_w_flat_ready;
    wire y_accept = out_valid && out_ready;
    wire do_shift = (state == S_RUN) && (y_accept || (realign_left != 2'd0));

    // ------------------------------------------------------------------------
    // Control
    // ------------------------------------------------------------------------
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state        <= S_LOAD;
            x_beat       <= 4'd0;
            w_loaded     <= 1'b0;
            filt         <= {FILT_W{1'b0}};
            pos          <= {POS_W{1'b0}};
            realign_left <= 2'd0;
        end else begin
            case (state)
                S_LOAD: begin
                    if (x_accept)
                        x_beat <= x_beat + 4'd1;
                    if (w_accept)
                        w_loaded <= 1'b1;

                    // Last beat may land on this edge; account for it.
                    if (((x_beat + (x_accept ? 4'd1 : 4'd0)) == X_BEATS[3:0]) &&
                        (w_loaded || w_accept)) begin
                        state        <= S_RUN;
                        filt         <= {FILT_W{1'b0}};
                        pos          <= {POS_W{1'b0}};
                        realign_left <= 2'd0;
                    end
                end

                S_RUN: begin
                    if (y_accept) begin
                        if (pos == OUT_POS[POS_W-1:0] - 1'b1) begin
                            if (filt == F[FILT_W-1:0] - 1'b1) begin
                                // Transaction complete. Ready for the next one.
                                state        <= S_LOAD;
                                x_beat       <= 4'd0;
                                w_loaded     <= 1'b0;
                                filt         <= {FILT_W{1'b0}};
                                pos          <= {POS_W{1'b0}};
                                realign_left <= 2'd0;
                            end else begin
                                filt         <= filt + 1'b1;
                                pos          <= {POS_W{1'b0}};
                                realign_left <= REALIGN[1:0];
                            end
                        end else begin
                            pos <= pos + 1'b1;
                        end
                    end else if (realign_left != 2'd0) begin
                        realign_left <= realign_left - 2'd1;
                    end
                end

                default: state <= S_LOAD;
            endcase
        end
    end

    // ------------------------------------------------------------------------
    // x shift-register: parallel lane load, else circular rotate-by-1
    // ------------------------------------------------------------------------
    genvar gi;
    generate
        for (gi = 0; gi < IN_LEN; gi = gi + 1) begin : g_xsr
            localparam integer BEAT = gi / LANES;
            localparam integer LANE = gi % LANES;
            always @(posedge clk) begin
                if (x_accept && (x_beat == BEAT[3:0]))
                    x_sr[gi] <= in_x_flat[LANE*DATA_W +: DATA_W];
                else if (do_shift) begin
                    if (gi == IN_LEN-1)
                        x_sr[gi] <= x_sr[0];
                    else
                        x_sr[gi] <= x_sr[gi+1];
                end
            end
        end
    endgenerate

    // ------------------------------------------------------------------------
    // weights: one beat, row-major w[f][k] at flat index f*K+k
    // ------------------------------------------------------------------------
    generate
        for (gi = 0; gi < W_ELEMS; gi = gi + 1) begin : g_w
            always @(posedge clk) begin
                if (w_accept)
                    w_mem[gi] <= in_w_flat[gi*DATA_W +: DATA_W];
            end
        end
    endgenerate

    // ------------------------------------------------------------------------
    // 4-tap signed MAC, one output per accepted cycle
    // ------------------------------------------------------------------------
    reg signed [DATA_W-1:0] sw0, sw1, sw2, sw3;
    always @* begin
        case (filt)
            2'd0: begin
                sw0 = w_mem[0];  sw1 = w_mem[1];  sw2 = w_mem[2];  sw3 = w_mem[3];
            end
            2'd1: begin
                sw0 = w_mem[4];  sw1 = w_mem[5];  sw2 = w_mem[6];  sw3 = w_mem[7];
            end
            2'd2: begin
                sw0 = w_mem[8];  sw1 = w_mem[9];  sw2 = w_mem[10]; sw3 = w_mem[11];
            end
            default: begin
                sw0 = w_mem[12]; sw1 = w_mem[13]; sw2 = w_mem[14]; sw3 = w_mem[15];
            end
        endcase
    end

    wire signed [DATA_W-1:0] sx0 = x_sr[0];
    wire signed [DATA_W-1:0] sx1 = x_sr[1];
    wire signed [DATA_W-1:0] sx2 = x_sr[2];
    wire signed [DATA_W-1:0] sx3 = x_sr[3];

    wire signed [2*DATA_W-1:0] m0 = sx0 * sw0;
    wire signed [2*DATA_W-1:0] m1 = sx1 * sw1;
    wire signed [2*DATA_W-1:0] m2 = sx2 * sw2;
    wire signed [2*DATA_W-1:0] m3 = sx3 * sw3;

    assign out_c = $signed(m0) + $signed(m1) + $signed(m2) + $signed(m3);

endmodule
